import streamlit as st
import pandas as pd
import sqlite3
import google.generativeai as genai
import json
import os
from datetime import datetime, timedelta
import tempfile
import time
import bcrypt
import plotly.express as px

# --- 1. アプリケーション設定 ---
st.set_page_config(
    page_title="IA家計簿 Pro",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="collapsed"
)

DB_FILE = "ai_kakeibo_pro.db"

# --- 2. データベース機能 ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Users
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash BLOB NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_username ON users(username)")
    
    # Receipts (1年分の詳細)
    c.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            year_month TEXT,
            date TEXT,
            shop TEXT,
            seq_no INTEGER,
            item_name TEXT,
            category TEXT,
            price INTEGER,
            cumulative_price INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mid_date ON receipts(user_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mid_ym ON receipts(user_id, year_month)")
    
    # Yearly History (30年保存)
    c.execute("""
        CREATE TABLE IF NOT EXISTS yearly_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            year TEXT,
            category TEXT,
            total_amount INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_yh_mid_year ON yearly_history(user_id, year)")
    
    # Schema Migration for 'total_amount'
    try:
        c.execute("ALTER TABLE yearly_history ADD COLUMN total_amount INTEGER")
    except:
        # Column likely already exists
        pass
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- 3. 認証システム ---
def check_password(password, hashed):
    return bcrypt.checkpw(password.encode('utf-8'), hashed)

def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

def login_page():
    st.title("🔐 AI家計簿 Pro - Login")
    tab1, tab2 = st.tabs(["ログイン", "新規登録"])
    
    with tab1:
        with st.form("login"):
            user = st.text_input("Username")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("ログイン", use_container_width=True):
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT * FROM users WHERE username = ?", (user,))
                u = c.fetchone()
                conn.close()
                if u and check_password(pw, u['password_hash']):
                    st.session_state.user_id = u['id']
                    st.session_state.username = u['username']
                    # 初期画面はホーム
                    st.session_state.current_view = 'home'
                    st.rerun()
                else:
                    st.error("認証失敗")
    
    with tab2:
        with st.form("register"):
            new_user = st.text_input("New Username")
            new_pw = st.text_input("New Password", type="password")
            if st.form_submit_button("登録", use_container_width=True):
                if new_user and new_pw:
                    try:
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
                                  (new_user, hash_password(new_pw)))
                        conn.commit()
                        conn.close()
                        st.success("登録完了。ログインしてください。")
                    except:
                        st.error("そのユーザー名は既に使用されています。")

# --- 4. 自動集計ロジック (Yearly Aggregation) ---
def update_yearly_history(user_id, receipt_date_str):
    try:
        dt = datetime.strptime(receipt_date_str, "%Y/%m/%d")
        if dt.month == 1:
            target_year = str(dt.year - 1)
            conn = get_db()
            c = conn.cursor()
            
            c.execute("SELECT 1 FROM yearly_history WHERE user_id = ? AND year = ?", (user_id, target_year))
            if not c.fetchone():
                c.execute("""
                    INSERT INTO yearly_history (user_id, year, category, total_amount)
                    SELECT user_id, substr(date, 1, 4) as year, category, SUM(price) as total_amount
                    FROM receipts
                    WHERE user_id = ? AND substr(date, 1, 4) = ?
                    GROUP BY category
                """, (user_id, target_year))
                conn.commit()
                if c.rowcount > 0:
                    st.toast(f"📅 前年({target_year})のデータを自動集計しました。")
            conn.close()
    except Exception as e:
        print(f"Aggregation Error: {e}")

# --- 5. AI解析 & データ保存 ---
def configure_genai():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
    if api_key:
        genai.configure(api_key=api_key)

def analyze_and_save(model_name, uploaded_file):
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    try:
        myfile = genai.upload_file(tmp_path)
        while myfile.state.name == "PROCESSING":
            time.sleep(1)
            myfile = genai.get_file(myfile.name)
            
        model = genai.GenerativeModel(model_name)
        prompt = """
        レシート画像を解析し、以下のJSON形式(List)のみを出力してください。
        
        [
            {
                "date": "YYYY/MM/DD", 
                "shop": "店舗名",
                "items": [
                    {"name": "商品名", "category": "カテゴリ", "price": 数値}
                ]
            }
        ]
        
        **ルール**
        1. 日付不明は本日。
        2. カテゴリ分類: 「飲料」「嗜好品」「酒」「お菓子」は必ず【食料品】に変換。
        3. 価格は数値のみ。
        """
        
        res = model.generate_content([myfile, prompt], generation_config={"response_mime_type": "application/json"})
        parsed = json.loads(res.text)
        
        conn = get_db()
        c = conn.cursor()
        user_id = st.session_state.user_id
        
        for receipt in parsed:
            date_str = receipt.get("date", datetime.now().strftime("%Y/%m/%d"))
            shop = receipt.get("shop", "不明")
            year_month = datetime.strptime(date_str, "%Y/%m/%d").strftime("%Y/%m") if date_str else "Unknown"
            
            curr_cumulative = 0
            
            for idx, item in enumerate(receipt.get("items", []), 1):
                price = int(item.get("price", 0))
                cat = item.get("category", "その他")
                if cat in ["飲料", "嗜好品"]: cat = "食料品"
                
                curr_cumulative += price
                
                c.execute("""
                    INSERT INTO receipts (user_id, year_month, date, shop, seq_no, item_name, category, price, cumulative_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, year_month, date_str, shop, idx, item.get("name"), cat, price, curr_cumulative))
            
            update_yearly_history(user_id, date_str)
            
        conn.commit()
        conn.close()
        return True
        
    except Exception as e:
        st.error(f"Error: {e}")
        return False
    finally:
        if os.path.exists(tmp_path): os.remove(tmp_path)

def predict_category(model_name, item_name):
    try:
        model = genai.GenerativeModel(model_name)
        prompt = f"""
        商品名「{item_name}」を以下のカテゴリから1つ選んで分類してください。
        回答はカテゴリ名のみを出力してください。
        
        カテゴリ候補:
        食料品, 日用品, 外食, 交通費, 交際費, 衣服・美容, 健康・医療, 通信費, 水道・光熱費, 住居費, 教育・教養, 娯楽, その他
        
        ※ルール:
        - アルコール、お菓子、飲料は「食料品」
        - 洗剤、ティッシュは「日用品」
        """
        response = model.generate_content(prompt)
        cat = response.text.strip()
        # クリーニング
        for c in ["食料品", "日用品", "外食", "交通費", "交際費", "衣服・美容", "健康・医療", "通信費", "水道・光熱費", "住居費", "教育・教養", "娯楽", "その他"]:
            if c in cat: return c
        return "その他"
    except:
        return "その他"

# --- 6. 画面コンポーネント ---

def show_home(username):
    st.title("🏠 AI家計簿 Pro - ホーム")
    col_title, col_help = st.columns([0.8, 0.2])
    with col_title:
        st.write(f"ようこそ、**{username}** さん ( Ver 1.02 )")
    with col_help:
        if st.button("❓ ヘルプ", use_container_width=True):
            st.session_state.current_view = 'help'
            st.rerun()
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.info("📝 手入力で登録")
        if st.button("手動入力フォームへ", use_container_width=True, type="primary"):
            st.session_state.current_view = 'manual'
            st.rerun()

    with col2:
        st.info("📁 レシートを撮影・選択")
        if st.button("写真orファイル選択", use_container_width=True, type="primary"):
            st.session_state.current_view = 'upload'
            st.rerun()
            
    with col3:
        st.info("📊 家計簿データを確認")
        if st.button("グラフ・集計を見る", use_container_width=True, type="primary"):
            st.session_state.current_view = 'dashboard'
            st.rerun()

def show_file_input(model_name):
    st.header("📁 写真またはファイル選択")
    if st.button("🏠 ホームに戻る"):
        st.session_state.current_view = 'home'
        st.rerun()
    
    uploaded_file = st.file_uploader("画像/動画を選択", type=['jpg','png','jpeg','mp4','mov'])
    
    if uploaded_file:
        st.markdown("**プレビュー**")
        is_video = uploaded_file.type.startswith('video')
        if is_video:
            st.video(uploaded_file)
        else:
            st.image(uploaded_file, use_container_width=True)
            
        if st.button("AI解析実行", type="primary", use_container_width=True):
            with st.spinner("AI解析中..."):
                if analyze_and_save(model_name, uploaded_file):
                    st.success("登録完了！")
                    time.sleep(1.5)
                    st.session_state.current_view = 'dashboard'
                    st.rerun()

def show_manual_input(model_name):
    st.header("📝 レシート手入力 (一括登録)")
    if st.button("🏠 ホームに戻る"):
        st.session_state.current_view = 'home'
        st.rerun()
        
    # Header inputs
    col1, col2 = st.columns(2)
    with col1:
        date_val = st.date_input("日付", value=datetime.now())
    with col2:
        shop_val = st.text_input("店舗名", placeholder="例: スーパーXX")

    st.caption("以下の表に商品名と金額を入力してください。行を追加して複数の商品を登録できます。")
    
    # Initialize data editor df
    if 'manual_df' not in st.session_state:
        st.session_state.manual_df = pd.DataFrame([{"商品名": "", "金額": 0}])

    edited_df = st.data_editor(
        st.session_state.manual_df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "商品名": st.column_config.TextColumn("商品名", required=True),
            "金額": st.column_config.NumberColumn("金額", format="%d 円", step=1, required=True)
        }
    )

    if st.button("一括登録する", type="primary", use_container_width=True):
        if not shop_val:
            st.error("店舗名を入力してください。")
            return

        valid_rows = edited_df[edited_df["商品名"].str.strip() != ""]
        valid_rows = valid_rows[valid_rows["金額"] != 0]
        
        if valid_rows.empty:
            st.error("商品名と金額（0以外）を入力してください。")
            return

        with st.spinner(f"{len(valid_rows)}件のデータをAI解析・登録中..."):
            user_id = st.session_state.user_id
            date_str = date_val.strftime("%Y/%m/%d")
            year_month = date_val.strftime("%Y/%m")
            
            conn = get_db()
            c = conn.cursor()
            
            curr_cumulative = 0
            
            for idx, row in valid_rows.iterrows():
                item_name = row["商品名"]
                price = int(row["金額"])
                
                # AI Category Prediction
                category = predict_category(model_name, item_name)
                
                curr_cumulative += price
                
                c.execute("""
                    INSERT INTO receipts (user_id, year_month, date, shop, seq_no, item_name, category, price, cumulative_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, year_month, date_str, shop_val, idx+1, item_name, category, price, curr_cumulative))
            
            update_yearly_history(user_id, date_str)
            conn.commit()
            conn.close()
            
            # Reset form
            st.session_state.manual_df = pd.DataFrame([{"商品名": "", "金額": 0}])
            st.success(f"{len(valid_rows)}件のデータを登録しました！")
            time.sleep(1.5)
            st.session_state.current_view = 'dashboard'
            st.rerun()

def show_dashboard():
    st.title("📊 ダッシュボード")
    if st.button("🏠 ホームに戻る"):
        st.session_state.current_view = 'home'
        st.rerun()

    user_id = st.session_state.user_id
    conn = get_db()
    
    # 今月データ
    today = datetime.now()
    this_month_str = today.strftime("%Y/%m")
    df_month = pd.read_sql("SELECT * FROM receipts WHERE user_id = ? AND year_month = ?", conn, params=(user_id, this_month_str))
    
    # 上部サマリー
    st.subheader(f"{this_month_str} の支出状況")
    if not df_month.empty:
        col1, col2 = st.columns(2)
        with col1:
            fig_pie = px.pie(df_month, values='price', names='category', hole=0.4, title="カテゴリ別割合")
            fig_pie.update_layout(showlegend=False, margin=dict(t=30,b=0,l=0,r=0))
            st.plotly_chart(fig_pie, use_container_width=True)
        with col2:
            daily_sum = df_month.groupby('date')['price'].sum().cumsum().reset_index()
            fig_area = px.area(daily_sum, x='date', y='price', title="日次累積推移")
            fig_area.update_layout(margin=dict(t=30,b=0,l=0,r=0))
            st.plotly_chart(fig_area, use_container_width=True)
    else:
        st.info("今月のデータはありません。")

    # タブ (順序変更: 一覧, 日別, 店舗別, 月別, 年別)
    tab_list, tab_date, tab_shop, tab_month, tab_year = st.tabs(["📝 一覧", "📆 日別", "🏢 店舗別", "📅 月別", "📉 年別"])
    
    # 1. 一覧
    with tab_list:
        st.caption("※直近1ヶ月以内のデータのみ削除可能")
        df_list = pd.read_sql("""
            SELECT date, shop, SUM(price) as total, MIN(created_at) as created_at
            FROM receipts WHERE user_id = ? 
            GROUP BY date, shop 
            ORDER BY date DESC, created_at DESC LIMIT 50
        """, conn, params=(user_id,))
        
        for _, r in df_list.iterrows():
            with st.expander(f"{r['date']} | {r['shop']} | ¥{r['total']:,}"):
                items = pd.read_sql("SELECT item_name, category, price FROM receipts WHERE user_id = ? AND date = ? AND shop = ?", conn, params=(user_id, r['date'], r['shop']))
                st.dataframe(items, use_container_width=True, hide_index=True)
                
                # 削除処理
                try:
                    rd = datetime.strptime(r['date'], "%Y/%m/%d")
                    if (datetime.now() - rd).days <= 30:
                        if st.button("削除", key=f"del_{r['date']}_{r['shop']}"):
                            c = conn.cursor()
                            c.execute("DELETE FROM receipts WHERE user_id = ? AND date = ? AND shop = ?", (user_id, r['date'], r['shop']))
                            conn.commit()
                            st.rerun()
                except: pass

    # Styling function
    def highlight_total(s):
        return ['background-color: #1f77b4; color: white; font-weight: bold' if s.name == '合計' else '' for _ in s]            

    # 2. 日別集計 (Category x Date)
    with tab_date:
        st.subheader("日別カテゴリー集計")
        if not df_month.empty:
            pivot_date = pd.pivot_table(df_month, index='category', columns='date', values='price', aggfunc='sum', fill_value=0)
            # 合計行追加
            pivot_date.loc['合計'] = pivot_date.sum(numeric_only=True)
            st.dataframe(pivot_date.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 3. 店舗別集計 (Category x Shop)
    with tab_shop:
        st.subheader("店舗別カテゴリー集計")
        if not df_month.empty:
            pivot_shop = pd.pivot_table(df_month, index='category', columns='shop', values='price', aggfunc='sum', fill_value=0)
            # 合計行追加
            pivot_shop.loc['合計'] = pivot_shop.sum(numeric_only=True)
            st.dataframe(pivot_shop.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 4. 月別集計 (Category x YearMonth)
    with tab_month:
        st.subheader("月別カテゴリー集計")
        df_all = pd.read_sql("SELECT category, year_month, price FROM receipts WHERE user_id = ?", conn, params=(user_id,))
        if not df_all.empty:
            pivot_month = pd.pivot_table(df_all, index='category', columns='year_month', values='price', aggfunc='sum', fill_value=0)
            # 合計行追加
            pivot_month.loc['合計'] = pivot_month.sum(numeric_only=True)
            st.dataframe(pivot_month.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 5. 年別集計 (Long Term)
    with tab_year:
        st.subheader("年別カテゴリー集計 (30年保存)")
        df_hist = pd.read_sql("SELECT * FROM yearly_history WHERE user_id = ? ORDER BY year", conn, params=(user_id,))
        if not df_hist.empty:
            fig_hist = px.bar(df_hist, x='year', y='total_amount', color='category')
            st.plotly_chart(fig_hist, use_container_width=True)
            
            pivot_hist = pd.pivot_table(df_hist, index='category', columns='year', values='total_amount', aggfunc='sum', fill_value=0)
            # 合計行追加
            pivot_hist.loc['合計'] = pivot_hist.sum(numeric_only=True)
            st.dataframe(pivot_hist.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("長期履歴なし")

    conn.close()

# --- 7. メイン処理 ---
def show_help(model_name):
    st.title("❓ AI家計簿 ヘルプチャット")
    if st.button("🏠 ホームに戻る"):
        st.session_state.current_view = 'home'
        st.rerun()

    # Chat history init
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # Chat input
    if prompt := st.chat_input("質問を入力してください（例：手入力の使い方は？）"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("AI回答作成中..."):
                try:
                    model = genai.GenerativeModel(model_name)
                    system_prompt = """
                    あなたは「AI家計簿 Pro」のヘルプアシスタントです。以下のアプリ機能に基づいてユーザーの質問に答えてください。
                    
                    【アプリ機能概要】
                    1. **ホーム画面**: 
                       - 「手入力で登録」、「写真またはファイル選択」、「グラフ・集計を見る」の3つの機能があります。
                       - 「ヘルプ」ボタンからこのチャットを開けます。
                    
                    2. **レシート手入力 (一括登録)**:
                       - 日付と店舗名を入力し、商品名と金額をリスト形式で複数追加できます。
                       - 「一括登録する」ボタンを押すと、AIが商品名から自動的にカテゴリ（食料品、日用品など）を判定して保存します。
                       - 金額にマイナスを入れると返品扱いになります。
                    
                    3. **写真またはファイル選択**:
                       - レシートの画像をアップロードすると、AIが内容を解析して自動登録します。
                    
                    4. **ダッシュボード**:
                       - **一覧**: 登録データの確認・削除ができます。
                       - **日別・店舗別・月別**: それぞれの切り口で集計表を表示します。合計行は青色で表示されます。
                       - **年別**: 過去30年分のデータをグラフで見ることができます。
                    
                    回答は親切で簡潔に、日本語で行ってください。
                    """
                    
                    response = model.generate_content([system_prompt, prompt])
                    reply = response.text
                    st.markdown(reply)
                    st.session_state.messages.append({"role": "assistant", "content": reply})
                except Exception as e:
                    error_msg = str(e).lower()
                    if "429" in error_msg or "quota" in error_msg:
                        st.warning("AIが少し混み合っています。7〜10秒ほど待ってから、もう一度送信してみてください。")
                    else:
                        st.error("申し訳ありません。一時的なエラーです。少し時間を置いてお試しください。")

def main():
    init_db()
    configure_genai()
    
    # ログインチェック
    if 'user_id' not in st.session_state:
        login_page()
        return

    # サイドバー (共通)
    with st.sidebar:
        st.header("設定")
        st.write(f"User: {st.session_state.username}")
        # Use available models from list
        model_name = st.selectbox("Model", ["gemini-2.0-flash", "gemini-pro-latest"])
        if st.button("ログアウト", type="primary"):
            st.session_state.clear()
            st.rerun()

    # ビューのルーティング
    if 'current_view' not in st.session_state:
        st.session_state.current_view = 'home'
        
    view = st.session_state.current_view
    
    if view == 'home':
        show_home(st.session_state.username)
    # camera view removed
    elif view == 'manual':
        show_manual_input(model_name)
    elif view == 'upload':
        show_file_input(model_name)
    elif view == 'dashboard':
        show_dashboard()
    elif view == 'help':
        show_help(model_name)

if __name__ == "__main__":
    main()
