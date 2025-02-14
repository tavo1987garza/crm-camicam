import sqlite3

def conectar_db():
    conn = sqlite3.connect("crm_camicam.db")
    return conn
