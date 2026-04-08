import streamlit as st
import pandas as pd
import sqlite3
import requests
from bs4 import BeautifulSoup
import zipfile
import xml.etree.ElementTree as ET
from fuzzywuzzy import fuzz
from nltk.stem import RSLPStemmer
import nltk
import datetime
import io
from openpyxl import Workbook
import schedule
import time
import threading

nltk.download('rslp', quiet=True)
stemmer = RSLPStemmer()

# -----------------------------------------------------------
# BANCO DE DADOS
# -----------------------------------------------------------

@st.cache_resource
def init_db():
    conn = sqlite3.connect('ip_guardian.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS portfolio
                    (processo TEXT PRIMARY KEY,
                     marca TEXT,
                     classe TEXT,
                     renewal_date TEXT,
                     status TEXT)''')

    conn.execute('''CREATE TABLE IF NOT EXISTS colisoes
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                     marca_port TEXT,
                     marca_rpi TEXT,
                     classe TEXT,
                     score INTEGER,
                     tipo TEXT,
                     data_check TEXT)''')

    conn.commit()
    return conn


# -----------------------------------------------------------
# SCRAPER DA RPI (INPI)
# -----------------------------------------------------------

def scrape_rpi():
    try:
        url = 'https://revistas.inpi.gov.br/rpi/'
        resp = requests.get(url)
        soup = BeautifulSoup(resp.content, 'html.parser')

        zip_links = [
            a['href'] for a in soup.find_all('a', href=True)
            if 'RM' in a.text and a['href'].endswith('.zip')
        ]

        if not zip_links:
            return []

        zip_url = 'https://revistas.inpi.gov.br' + zip_links[0]
        r = requests.get(zip_url)

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for file in z.namelist():
                if file.endswith('.xml'):
                    xml_data = z.read(file)
                    root = ET.fromstring(xml_data)

                    ns = {'inpi': 'http://www.inpi.gov.br'}

                    rpi_marcas = []
                    for elem in root.findall('.//inpi:marcas', ns):
                        marca = elem.find('inpi:denominacao', ns)
                        classe = elem.find('inpi:classeNCL', ns)

                        if marca is not None and classe is not None:
                            nome = marca.text.lower() if marca.text else ""
                            rpi_marcas.append({"marca": nome, "classe": classe.text})

                    return rpi_marcas

    except Exception as e:
        st.error(f"Erro ao acessar RPI: {e}")
        return []


# -----------------------------------------------------------
# COLISÕES
# -----------------------------------------------------------

def compute_colisoes(conn):
    df_port = pd.read_sql('SELECT * FROM portfolio', conn)
    rpi = scrape_rpi()

    colis = []
    for _, row in df_port.iterrows():
        nome_p = row['marca'].lower()
        cl_p = row['classe']

        for rp in rpi:
            if cl_p == rp['classe']:
                nome_r = rp['marca']
                score = 0
                tipo = ""

                if nome_p == nome_r:
                    score, tipo = 100, "Exata"
                elif nome_p.startswith(nome_r) or nome_r.startswith(nome_p):
                    score, tipo = 90, "Prefixo"
                elif nome_p.endswith(nome_r) or nome_r.endswith(nome_p):
                    score, tipo = 85, "Sufixo"
                elif fuzz.ratio(nome_p, nome_r) > 80:
                    score, tipo = 80, "Radical"

                if score >= 80:
                    colis.append((row['marca'], nome_r, cl_p, score, tipo,
                                  datetime.date.today().isoformat()))

    if colis:
        df = pd.DataFrame(colis, columns=[
            'marca_port', 'marca_rpi', 'classe', 'score', 'tipo', 'data_check'
        ])
        df.to_sql('colisoes', conn, if_exists='append', index=False)


# -----------------------------------------------------------
# DASHBOARD
# -----------------------------------------------------------

st.set_page_config(page_title="IP GUARDIAN", layout="wide")
st.title("🛡️ **IP GUARDIAN** — Sistema de Gestão de Marcas (INPI/RPI)")

conn = init_db()

tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "💼 Portfolio", "🚨 Colidências & Alertas"])


# -----------------------------------------------------------
# TAB 1 — DASHBOARD
# -----------------------------------------------------------

with tab1:
    total = pd.read_sql('SELECT COUNT(*) FROM portfolio', conn).iloc[0, 0]
    st.metric("Total de Marcas", total)

    hoje = datetime.date.today()
    alertas = pd.read_sql(
        f"""
        SELECT * FROM portfolio
        WHERE julianday(renewal_date) - julianday('{hoje}') < 60
        """,
        conn
    )

    st.metric("Alertas de Renovação (< 60 dias)", len(alertas))
    st.dataframe(alertas)


# -----------------------------------------------------------
# TAB 2 — PORTFOLIO
# -----------------------------------------------------------

with tab2:
    uploaded = st.file_uploader("Importar Excel", type=["xlsx"])

    if uploaded:
        df = pd.read_excel(uploaded)
        df.to_sql("portfolio", conn, if_exists="replace", index=False)
        st.success(f"{len(df)} marcas importadas com sucesso!")

    st.subheader("Adicionar Nova Marca")

    with st.form("form_add"):
        c1, c2 = st.columns(2)
        with c1:
            processo = st.text_input("Processo")
            marca = st.text_input("Marca")
            classe = st.text_input("Classe Nice")

        with c2:
            renewal = st.date_input("Data de Renovação")
            status = st.selectbox("Status",
                                  ["Ativa", "Registrada", "Pedido", "Suspensa"])

        submitted = st.form_submit_button("Salvar")

        if submitted:
            df_add = pd.DataFrame([{
                "processo": processo,
                "marca": marca,
                "classe": classe,
                "renewal_date": renewal,
                "status": status
            }])
            df_add.to_sql("portfolio", conn, if_exists="append", index=False)
            st.success("Marca salva!")
            st.experimental_rerun()

    st.subheader("Marcas Cadastradas")
    st.dataframe(pd.read_sql("SELECT * FROM portfolio", conn))


# -----------------------------------------------------------
# TAB 3 — COLISÕES
# -----------------------------------------------------------

with tab3:
    if st.button("🔍 Verificar Colidências (RPI)"):
        compute_colisoes(conn)
        st.success("Colidências atualizadas!")

    df_col = pd.read_sql("SELECT * FROM colisoes ORDER BY score DESC", conn)
    st.dataframe(df_col)

    if st.button("📥 Exportar Relatório Excel"):
        wb = Workbook()
        ws = wb.active
        ws.title = "Colisoes"

        df = df_col
        ws.append(list(df.columns))
        for row in df.values:
            ws.append(list(row))

        bio = io.BytesIO()
        wb.save(bio)

        st.download_button("Baixar Excel",
                           bio.getvalue(),
                           "IP_GUARDIAN_Relatorio.xlsx")


# -----------------------------------------------------------
# SCHEDULER (autochecagem semanal)
# -----------------------------------------------------------

def run_scheduler():
    schedule.every().tuesday.at("09:00").do(lambda: compute_colisoes(conn))
    while True:
        schedule.run_pending()
        time.sleep(60)


threading.Thread(target=run_scheduler, daemon=True).start()