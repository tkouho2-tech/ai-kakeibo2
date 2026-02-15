import sqlite3
try:
    conn = sqlite3.connect("ai_kakeibo_pro.db")
    c = conn.cursor()
    c.execute("PRAGMA table_info(yearly_history)")
    columns = c.fetchall()
    print("Columns in yearly_history:")
    found = False
    for col in columns:
        print(f"- {col[1]} ({col[2]})")
        if col[1] == 'total_amount':
            found = True
    
    if not found:
        print("MISSING: total_amount column")
    else:
        print("FOUND: total_amount column")
    conn.close()
except Exception as e:
    print(e)
