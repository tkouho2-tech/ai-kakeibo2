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
from PIL import Image
import io
import calendar
import streamlit.components.v1 as components

# --- 1. アプリケーション設定 ---
st.set_page_config(
    page_title="AI家計簿 Ver 3.04",
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
            image_path TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mid_date ON receipts(user_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mid_ym ON receipts(user_id, year_month)")
    
    # Schema Migration for 'image_path'
    try:
        c.execute("ALTER TABLE receipts ADD COLUMN image_path TEXT")
    except:
        pass
    
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

# --- 4.5 画像処理 ---
def resize_image(image_file):
    try:
        img = Image.open(image_file)
        # Convert to RGB if necessary (e.g. for RGBA/P palette)
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        # Resize if long edge > 1280
        max_size = 1280
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            
        # Save to buffer
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return buf
    except Exception as e:
        st.error(f"画像処理エラー: {e}")
        return None

# --- 5. AI解析 & データ保存 ---
def configure_genai():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
    if api_key:
        genai.configure(api_key=api_key)

def analyze_and_save(model_name, uploaded_file):
    # 1. 画像リサイズ & 軽量化
    resized_image_buffer = resize_image(uploaded_file)
    if not resized_image_buffer: return False
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        tmp.write(resized_image_buffer.getvalue())
        tmp_path = tmp.name

    try:
        myfile = genai.upload_file(tmp_path, mime_type="image/jpeg")
        while myfile.state.name == "PROCESSING":
            time.sleep(1)
            myfile = genai.get_file(myfile.name)
            
        model = genai.GenerativeModel(model_name)
        prompt = """
        レシートまたは医療費領収書画像を解析し、以下のJSON形式(List)のみを出力してください。
        
        [
            {
                "date": "YYYY/MM/DD", 
                "shop": "店舗名または病院・薬局名",
                "is_medical": boolean,
                "items": [
                    {"name": "商品名または摘要", "category": "カテゴリ", "price": 数値}
                ]
            }
        ]
        
        **解析ルール**
        1. **日付**: 不明な場合は本日。
        2. **医療費判定**: 
           - 「領収証」「診療費」「保険薬局」などの記載がある場合は医療費とみなす。
           - 場所(shop)は病院名・クリニック名・薬局名にする。
           - 医療費の場合、個別の点数ではなく「請求金額(合計)」を1つの項目として抽出する。商品名は「外来診療費」や「薬剤費」などにする。
           - カテゴリは必ず「医療・健康」にする。
        3. **通常レシート**:
           - カテゴリ分類: 「飲料」「嗜好品」「酒」「お菓子」は必ず【食料品】に変換。
           - 価格は数値のみ。
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
                # 医療費フラグがあればカテゴリ強制
                if receipt.get("is_medical", False):
                    cat = "医療・健康"
                else:
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
        error_msg = str(e)
        if "429" in error_msg:
            st.warning("⚠️ AIが混み合っています。10秒ほど待ってから再度お試しください。")
        else:
            st.error(f"解析エラー: {e}")
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
        st.write(f"ようこそ、**{username}** さん ( Ver 3.04 )")
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
    
    uploaded_file = st.file_uploader("画像/動画を選択", type=['jpg','png','jpeg','mp4','mov', 'heic', 'heif'])
    
    if uploaded_file:
        st.markdown("**プレビュー**")
        is_video = uploaded_file.type.startswith('video')
        if is_video:
            st.video(uploaded_file)
        else:
            if uploaded_file.name.lower().endswith(('.heic', '.heif')):
                st.warning("⚠️ HEIC形式のプレビューはサポートされていませんが、解析は可能です。")
            else:
                st.image(uploaded_file, use_container_width=True)
            
        if st.button("AI解析実行", type="primary", use_container_width=True):
            with st.spinner("AIがレシートを解析しています..."):
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

# --- 6. v3.0 Modern Dashboard Components ---

def get_category_color(category):
    colors = {
        "食料品": "#a8e6a3", # Thin Green
        "外食": "#ffcc80",   # Orange
        "日用品": "#90caf9", # Blue
        "交通費": "#ce93d8", # Purple
        "医療・健康": "#81d4fa", # Light Blue
        "住居費": "#bcaaa4",
        "水道・光熱費": "#ffab91",
        "通信費": "#e6ee9c",
        "衣服・美容": "#f48fb1",
        "交際費": "#b39ddb",
        "教育・教養": "#80cbc4",
        "娯楽": "#ffff8d",
        "その他": "#eeeeee"
    }
    return colors.get(category, "#eeeeee")

def render_calendar(df_month, year, month):
    # Calendar implementation
    st.subheader(f"{year}年{month}月 カレンダー")
    
    # helper for date normalization
    def normalize_date(d):
        return str(d).replace('-', '/')

    # Create daily totals dict
    daily_totals = {}
    if not df_month.empty:
        temp_df = df_month.copy()
        temp_df['date_str'] = temp_df['date'].apply(normalize_date)
        daily_totals = temp_df.groupby('date_str')['price'].sum().to_dict()
    
    # Calendar module setup
    cal = calendar.Calendar(firstweekday=6) # Sunday start
    month_days = cal.monthdayscalendar(year, month)
    
    # Custom CSS and Grid HTML Construction
    css = """
        <style>
        body { margin: 0; padding: 0; font-family: sans-serif; }
        .cal-container {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 5px;
            padding-bottom: 20px;
        }
        .cal-header {
            text-align: center;
            font-weight: bold;
            padding: 5px;
            font-size: 0.9rem;
        }
        .cal-cell {
            background-color: #ffffff;
            border: 1px solid #ddd;
            border-radius: 5px;
            height: 80px;
            position: relative;
            cursor: pointer;
            transition: background-color 0.2s;
            box-sizing: border-box;
        }
        .cal-cell:hover {
            background-color: #f9f9f9;
            border-color: #bbb;
        }
        .cal-cell-link {
            text-decoration: none;
            color: inherit;
            display: block;
            width: 100%;
            height: 100%;
        }
        .cal-date-label {
            position: absolute;
            top: 5px;
            left: 8px;
            font-weight: bold;
            font-size: 1rem;
            color: #333;
        }
        .cal-price-label {
            position: absolute;
            bottom: 5px;
            right: 8px;
            color: #d32f2f;
            font-weight: bold;
            font-size: 0.9rem;
        }
        </style>
    """
    
    # Headers
    weekdays = ["日", "月", "火", "水", "木", "金", "土"]
    colors = ["#d32f2f", "#333", "#333", "#333", "#333", "#333", "#1976d2"]
    
    # Build HTML for Grid
    calendar_html = css + '<div class="cal-container">'
    
    # Header Row
    for i, w in enumerate(weekdays):
        calendar_html += f'<div class="cal-header" style="color:{colors[i]};">{w}</div>'
    
    # Days Rows
    for week in month_days:
        for day in week:
            if day == 0:
                calendar_html += '<div class="cal-cell" style="border:none; background:transparent; cursor:default;"></div>'
            else:
                date_str = f"{year}/{month:02d}/{day:02d}"
                total = daily_totals.get(date_str, 0)
                
                price_html = f'<div class="cal-price-label">¥{total:,}</div>' if total > 0 else ""
                
                # Link to same page with query param
                # Note: target="_top" is REQUIRED for components.html to escape iframe
                calendar_html += f"""
                <a href="?sel_date={date_str}" target="_top" class="cal-cell-link">
                    <div class="cal-cell">
                        <div class="cal-date-label">{day}</div>
                        {price_html}
                    </div>
                </a>
                """
    
    calendar_html += '</div>'
    
    # --- Add Detail View logic back to render_calendar ---
    # This will be appended to the HTML generated by components.html
    if 'selected_cal_date' in st.session_state:
        selected_date = st.session_state['selected_cal_date']
        try:
            sel_y, sel_m, _ = map(int, selected_date.split('/'))
            if sel_y == st.session_state.view_date.year and sel_m == st.session_state.view_date.month:
                df_detail = df_month.copy()
                df_detail['temp_date_str'] = df_detail['date'].astype(str).str.replace('-', '/')
                day_data = df_detail[df_detail['temp_date_str'] == selected_date]
                
                if not day_data.empty:
                    day_total = day_data['price'].sum()
                    summary_df = day_data.groupby(['category', 'shop'])['price'].sum().reset_index().sort_values('price', ascending=False)
                    
                    detail_html = f"""
                    <div class="detail-box">
                        <div class="detail-header">
                            <span>📅 {selected_date} の詳細</span>
                            <span style="color:#d32f2f;">合計: ¥{day_total:,}</span>
                        </div>
                    """
                    for _, row in summary_df.iterrows():
                        cat = row['category']
                        shop = row['shop'] if row['shop'] else "不明"
                        price = row['price']
                        label = f"{cat}（{shop}）"
                        detail_html += f"""
                        <div class="detail-item">
                            <span style="font-weight:bold; color:#333;">{label}</span>
                            <span style="font-weight:bold; color:#333;">¥{price:,}</span>
                        </div>
                        """
                    detail_html += "</div>"
                    calendar_html += detail_html
                else:
                    calendar_html += f"<div class='detail-box'><div class='detail-header'><span>📅 {selected_date} の詳細</span></div><p>支出記録はありません。</p></div>"
        except Exception as e:
            # Handle potential errors in date parsing or data processing
            calendar_html += f"<div class='detail-box'><div class='detail-header'><span>📅 {selected_date} の詳細</span></div><p>詳細の読み込み中にエラーが発生しました: {e}</p></div>"

    # Render using components.html as recommended
    components.html(calendar_html, height=600, scrolling=True)
    
def show_dashboard():
    # --- Handle Query Params for Date Selection (Runs first) ---
    qp_date = None
    try:
        # Streamlit >= 1.30
        if "sel_date" in st.query_params:
            qp_date = st.query_params["sel_date"]
    except:
        try:
            # Fallback for older Streamlit
            qps = st.experimental_get_query_params()
            if "sel_date" in qps:
                qp_date = qps["sel_date"][0] 
        except:
            pass
            
    if qp_date:
        st.session_state['selected_cal_date'] = qp_date
        # Force update view_date to match selected date so data is loaded
        try:
             dt_sel = datetime.strptime(qp_date, "%Y/%m/%d")
             st.session_state['view_date'] = dt_sel
        except:
             pass

    # CSS Injection for Modern Theme and List
    st.markdown("""
        <style>
        /* Modern Clean Theme */
        .stApp {
            background-color: #ffffff;
            color: #333333;
            font-family: 'Helvetica Neue', Arial, sans-serif;
        }
        /* Month Navigation */
        .month-nav {
            display: flex;
            justify-content: center;
            align-items: center;
            font-size: 1.2rem;
            font-weight: bold;
            color: #555;
            margin-bottom: 20px;
        }
        /* Summary Total */
        .summary-total {
            text-align: center;
            font-size: 1.5rem;
            font-weight: bold;
            color: #333;
            margin-top: -10px;
            margin-bottom: 20px;
        }
        /* Category List */
        .cat-list-item {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 15px 10px; /* Comfortable touch padding */
            border-bottom: 1px solid #e0e0e0;
        }
        .cat-icon {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            margin-right: 15px;
            flex-shrink: 0;
        }
        .cat-name {
            flex-grow: 1;
            font-size: 1rem;
            color: #444;
        }
        .cat-price {
            font-weight: bold;
            color: #333;
            font-size: 1rem;
        }
        /* Hide default Streamlit elements */
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        </style>
    """, unsafe_allow_html=True)

    st.title("📊 ダッシュボード Ver 3.04")
    if st.button("🏠 ホームに戻る"):
        st.session_state.current_view = 'home'
        st.rerun()

    # State Management for Month
    if 'view_date' not in st.session_state:
        st.session_state.view_date = datetime.now()

    # Month Navigation
    col_prev, col_curr, col_next = st.columns([1, 4, 1])
    with col_prev:
        if st.button("<", key="prev_mo"):
            first = st.session_state.view_date.replace(day=1)
            st.session_state.view_date = first - timedelta(days=1)
            st.rerun()
    with col_curr:
        curr_str = st.session_state.view_date.strftime("%Y年 %m月")
        st.markdown(f"<div class='month-nav'>{curr_str}</div>", unsafe_allow_html=True)
    with col_next:
        if st.button(">", key="next_mo"):
            first = st.session_state.view_date.replace(day=1)
            next_month = (first + timedelta(days=32)).replace(day=1)
            st.session_state.view_date = next_month
            st.rerun()

    user_id = st.session_state.user_id
    conn = get_db()
    
    # Get Data for Selected Month
    view_ym = st.session_state.view_date.strftime("%Y/%m")
    df_month = pd.read_sql("SELECT * FROM receipts WHERE user_id = ? AND year_month = ?", conn, params=(user_id, view_ym))
    
    # --- Donut Chart & List Section ---
    total_exp = 0
    if not df_month.empty:
        total_exp = df_month['price'].sum()
        
        # Aggregate by category for chart
        df_cat = df_month.groupby('category')['price'].sum().reset_index()
        
        # Donut Chart
        fig = px.pie(df_cat, values='price', names='category', hole=0.5,
                     color='category',
                     color_discrete_map={c: get_category_color(c) for c in df_cat['category'].unique()})
        fig.update_traces(textinfo='none', hoverinfo='label+percent+value')
        fig.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=280)
        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
        
        # Total Summary
        st.markdown(f"<div class='summary-total'>合計 ¥{total_exp:,}</div>", unsafe_allow_html=True)
        
        # Detailed Category List
        df_cat = df_cat.sort_values('price', ascending=False)
        st.markdown("<div style='border-top: 1px solid #e0e0e0;'>", unsafe_allow_html=True) # Top border
        for _, row in df_cat.iterrows():
            cat = row['category']
            price = row['price']
            color = get_category_color(cat)
            st.markdown(f"""
            <div class='cat-list-item'>
                <div style='display:flex; align-items:center; flex-grow:1;'>
                    <div class='cat-icon' style='background-color: {color};'></div>
                    <div class='cat-name'>{cat}</div>
                </div>
                <div class='cat-price'>¥{price:,}</div>
            </div>
            """, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True) # Close container
        
    else:
        st.info(f"{view_ym} のデータはありません。")
        # Empty Donut
        fig = px.pie(values=[1], names=['なし'], hole=0.5, color_discrete_sequence=['#eeeeee'])
        fig.update_traces(textinfo='none', hoverinfo='skip')
        fig.update_layout(showlegend=False, margin=dict(t=10, b=10, l=10, r=10), height=280)
        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
        st.markdown(f"<div class='summary-total'>合計 ¥0</div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # --- Tabs Section ---
    # Dynamic Tab Ordering: Put Calendar First if date is selected
    default_tabs = ["📝 一覧", "📆 日別", "📅 カレンダー", "🏢 店舗別", "🌙 月別", "📉 年別"]
    
    # Check if we should prioritize calendar
    if qp_date:
        # Move Calendar to front
        if "📅 カレンダー" in default_tabs: # Ensure it exists before removing
            default_tabs.remove("📅 カレンダー")
        default_tabs.insert(0, "📅 カレンダー")
        
    # Create Tabs
    tabs = st.tabs(default_tabs)
    
    # Map tabs to content
    tab_map = {name: tab for name, tab in zip(default_tabs, tabs)}
    
    # 1. List Tab
    with tab_map["📝 一覧"]:
        st.caption(f"{view_ym} の明細 (※カテゴリ修正可)")
        if not df_month.empty:
            receipt_cols = ['date', 'shop', 'created_at']
            df_receipts = df_month[receipt_cols].drop_duplicates().sort_values(['date', 'created_at'], ascending=[False, False])
            
            for _, r in df_receipts.iterrows():
                mask = (
                    (df_month['date'] == r['date']) & 
                    (df_month['shop'] == r['shop']) & 
                    (df_month['created_at'] == r['created_at'])
                )
                df_items = df_month[mask].copy()
                if df_items.empty: continue
                total_receipt = df_items['price'].sum()
                receipt_key = f"{r['date']}_{r['shop']}_{r['created_at']}"
                
                with st.expander(f"{r['date']} | {r['shop']} | ¥{total_receipt:,}"):
                    with st.form(key=f"form_{receipt_key}"):
                        reset_key_name = f"reset_{receipt_key}"
                        if reset_key_name not in st.session_state: st.session_state[reset_key_name] = 0
                        edit_target = df_items[['id', 'item_name', 'price', 'category']].copy()
                        editor_key = f"editor_{receipt_key}_{st.session_state[reset_key_name]}"
                        edited_df = st.data_editor(
                            edit_target, key=editor_key,
                            column_config={
                                "id": None,
                                "item_name": st.column_config.TextColumn("商品名", disabled=True),
                                "price": st.column_config.NumberColumn("金額", format="¥%d", disabled=True),
                                "category": st.column_config.SelectboxColumn("カテゴリ", options=["食料品", "外食", "日用品", "交通費", "医療・健康", "住居費", "水道・光熱費", "通信費", "衣服・美容", "交際費", "教育・教養", "娯楽", "その他"], required=True)
                            }, hide_index=True, use_container_width=True
                        )
                        # Re-implementation of form buttons to ensure strict nesting
                        c1, c2, c3 = st.columns([1, 1, 1])
                        
                        with c1:
                            if st.form_submit_button("変更を保存", type="primary"):
                                c = conn.cursor()
                                for _, row in edited_df.iterrows():
                                    c.execute("UPDATE receipts SET category = ? WHERE id = ?", (row['category'], row['id']))
                                conn.commit()
                                st.session_state[reset_key_name] += 1
                                st.toast("✅ カテゴリを保存しました")
                                time.sleep(1)
                                st.rerun()
                                
                        with c2:
                            if st.form_submit_button("元に戻す"):
                                st.session_state[reset_key_name] += 1
                                st.rerun()
                                
                        with c3:
                            if st.form_submit_button("🗑️ 削除"):
                                c = conn.cursor()
                                ids = tuple(df_items['id'].tolist())
                                if len(ids) == 1:
                                    c.execute("DELETE FROM receipts WHERE id = ?", (ids[0],))
                                else:
                                    c.execute(f"DELETE FROM receipts WHERE id IN ({','.join(['?']*len(ids))})", ids)
                                conn.commit()
                                st.toast("🗑️ 削除しました")
                                time.sleep(1)
                                st.rerun()
        else:
            st.info("データがありません")

    # Styling function
    def highlight_total(s):
        return ['background-color: #1f77b4; color: white; font-weight: bold' if s.name == '合計' else '' for _ in s]            

    # 2. 日別集計 (Category x Date) - Filtered by selected month
    with tab_map["📆 日別"]:
        st.subheader("日別カテゴリー集計")
        if not df_month.empty:
            pivot_date = pd.pivot_table(df_month, index='category', columns='date', values='price', aggfunc='sum', fill_value=0)
            pivot_date.loc['合計'] = pivot_date.sum(numeric_only=True)
            st.dataframe(pivot_date.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 3. Calendar (New!)
    with tab_map["📅 カレンダー"]:
        if not df_month.empty:
            render_calendar(df_month, st.session_state.view_date.year, st.session_state.view_date.month)
        else:
            st.info("データがありません")

    # 4. 店舗別集計 (Category x Shop)
    with tab_map["🏢 店舗別"]:
        st.subheader("店舗別カテゴリー集計")
        if not df_month.empty:
            pivot_shop = pd.pivot_table(df_month, index='category', columns='shop', values='price', aggfunc='sum', fill_value=0)
            pivot_shop.loc['合計'] = pivot_shop.sum(numeric_only=True)
            st.dataframe(pivot_shop.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 5. 月別集計 (Category x YearMonth) - All time
    with tab_map["🌙 月別"]:
        st.subheader("月別カテゴリー集計 (全期間)")
        df_all = pd.read_sql("SELECT category, year_month, price FROM receipts WHERE user_id = ?", conn, params=(user_id,))
        if not df_all.empty:
            pivot_month = pd.pivot_table(df_all, index='category', columns='year_month', values='price', aggfunc='sum', fill_value=0)
            pivot_month.loc['合計'] = pivot_month.sum(numeric_only=True)
            st.dataframe(pivot_month.style.apply(highlight_total, axis=1), use_container_width=True)
        else:
            st.info("データがありません")

    # 6. 年別集計 (Long Term)
    with tab_map["📉 年別"]:
        st.subheader("年別カテゴリー集計 (30年保存)")
        df_hist = pd.read_sql("SELECT * FROM yearly_history WHERE user_id = ? ORDER BY year", conn, params=(user_id,))
        if not df_hist.empty:
            fig_hist = px.bar(df_hist, x='year', y='total_amount', color='category')
            st.plotly_chart(fig_hist, use_container_width=True)
            
            pivot_hist = pd.pivot_table(df_hist, index='category', columns='year', values='total_amount', aggfunc='sum', fill_value=0)
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
        # Use authenticated models
        model_name = st.selectbox("Model", ["gemini-2.5-flash", "gemini-2.0-flash"])
        if st.button("ログアウト", type="primary"):
            st.session_state.clear()
            st.rerun()
            
            st.session_state.current_view = 'upload'
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
    # v3.0: dashboard is home
    elif view == 'dashboard':
        show_dashboard()
    elif view == 'help':
        show_help(model_name)

if __name__ == "__main__":
    main()
