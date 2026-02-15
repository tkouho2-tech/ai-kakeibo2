import streamlit as st
import pandas as pd
import sqlite3
import google.generativeai as genai
import json
import os
from datetime import datetime
import tempfile
import time

# --- 1. アプリケーション設定 ---
st.set_page_config(
    page_title="AI家計簿アプリ",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_FILE = "kakeibo.db"

# --- 2. データベース機能 ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS receipt_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year_month TEXT,
            date TEXT,
            location TEXT,
            sequence_num INTEGER,
            item_name TEXT,
            category TEXT,
            price INTEGER,
            cumulative_price INTEGER
        )
    """)
    conn.commit()
    conn.close()

def save_to_db(df):
    conn = sqlite3.connect(DB_FILE)
    # DataFrameをそのままDBへ追記
    df.to_sql("receipt_data", conn, if_exists="append", index=False)
    conn.close()

def load_data():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql("SELECT * FROM receipt_data", conn)
    conn.close()
    return df

# --- 3. Gemini API連携 ---
def configure_genai():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
    
    if not api_key:
        st.error("Gemini APIキーが設定されていません。st.secretsまたは環境変数を確認してください。")
        st.stop()
    
    genai.configure(api_key=api_key)

def analyze_receipt(model_name, uploaded_file):
    # 一時ファイルとして保存
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp_file:
        tmp_file.write(uploaded_file.getvalue())
        tmp_path = tmp_file.name

    try:
        # ファイルアップロード
        myfile = genai.upload_file(tmp_path)
        
        # 動画の場合は処理完了を待機
        while myfile.state.name == "PROCESSING":
            time.sleep(2)
            myfile = genai.get_file(myfile.name)

        model = genai.GenerativeModel(model_name)
        
        prompt = """
        レシートの画像を解析し、以下の情報をJSON形式で抽出してください。
        
        **出力フォーマット (JSON List):**
        [
            {
                "date": "YYYY/MM/DD", 
                "location": "店舗名",
                "items": [
                    {"name": "商品名", "category": "カテゴリ", "price": 価格(数値)}
                ]
            }
        ]
        
        **ルール:**
        1. 日付はYYYY/MM/DD形式に統一してください。不明な場合は本日の日付を使用してください。
        2. カテゴリについて:
           - 「飲料」または「嗜好品」と思われるものは、必ず「食料品」として出力してください。
           - それ以外は一般的な家計簿カテゴリ（例: 食料品, 日用品, 交通費, 衣服, 交際費 等）を推測してください。
        3. 価格はエン記号などを除いた整数値にしてください。
        4. 画像内に複数のレシートがある場合は、リスト形式で複数のオブジェクトを返してください。
        """

        response = model.generate_content(
            [myfile, prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        
        return json.loads(response.text)

    except Exception as e:
        st.error(f"解析エラー: {e}")
        return None
    finally:
        # 一時ファイルの削除
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- 4. データ加工 ---
def process_extracted_data(extracted_json):
    rows = []
    
    for receipt in extracted_json:
        date_str = receipt.get("date", datetime.now().strftime("%Y/%m/%d"))
        location = receipt.get("location", "不明")
        try:
            year_month = datetime.strptime(date_str, "%Y/%m/%d").strftime("%Y/%m")
        except:
            year_month = datetime.now().strftime("%Y/%m") # パース失敗時のフォールバック

        current_cumulative = 0
        for idx, item in enumerate(receipt.get("items", []), 1):
            price = int(item.get("price", 0))
            category = item.get("category", "未分類")
            
            # カテゴリ正規化 (念のため再確認)
            if category in ["飲料", "嗜好品"]:
                category = "食料品"
            
            current_cumulative += price
            
            rows.append({
                "year_month": year_month,
                "date": date_str,
                "location": location,
                "sequence_num": idx,
                "item_name": item.get("name", "不明"),
                "category": category,
                "price": price,
                "cumulative_price": current_cumulative
            })
            
    return pd.DataFrame(rows)

# --- 5. メインUI ---
def main():
    init_db()
    configure_genai()

    st.title("💰 AI家計簿アプリ")
    
    # サイドバー
    with st.sidebar:
        st.header("設定 & 操作")
        model_option = st.selectbox("使用モデル", ["gemini-flash-latest", "gemini-pro-latest"])
        st.markdown("---")
        st.markdown("### 使い方")
        st.markdown("1. レシート画像/動画をアップロード\n2. AI解析を実行\n3. 結果を確認してDB保存\n4. データ分析タブで確認")

    # メインタブ
    tab1, tab2, tab3 = st.tabs(["📤 データ登録", "📊 ダッシュボード", "📂 データ管理"])

    # タブ1: データ登録
    with tab1:
        st.header("レシート解析")
        uploaded_file = st.file_uploader("レシートの画像または動画をアップロード", type=["jpg", "jpeg", "png", "mp4"])
        
        if uploaded_file:
            # プレビュー
            if uploaded_file.type.startswith("image"):
                st.image(uploaded_file, caption="アップロード画像", use_column_width=True)
            elif uploaded_file.type.startswith("video"):
                st.video(uploaded_file)
            
            if st.button("AI解析開始", type="primary"):
                with st.spinner("AIがレシートを解析中..."):
                    extracted_data = analyze_receipt(model_option, uploaded_file)
                    
                    if extracted_data:
                        df_new = process_extracted_data(extracted_data)
                        st.session_state["preview_df"] = df_new
                        st.success("解析完了！")
        
        # 解析結果の確認と保存
        if "preview_df" in st.session_state:
            st.subheader("解析結果プレビュー")
            edited_df = st.data_editor(st.session_state["preview_df"], num_rows="dynamic")
            
            col1, col2 = st.columns(2)
            with col1:
                if st.button("保存する"):
                    save_to_db(edited_df)
                    st.success("データベースに保存しました！")
                    del st.session_state["preview_df"] # 保存後はクリア
                    st.rerun()
            with col2:
                if st.button("キャンセル"):
                    del st.session_state["preview_df"]
                    st.rerun()

    # タブ2: ダッシュボード
    with tab2:
        st.header("家計簿分析")
        df = load_data()
        
        if not df.empty:
            # ピボットテーブル分析用のサブタブ
            pivot_tab1, pivot_tab2, pivot_tab3, pivot_tab4 = st.tabs([
                "📍 場所 vs 日付", "📅 年月別", "📆 日付別", "🏢 場所別"
            ])
            
            # 1. 場所 VS 日付
            with pivot_tab1:
                st.subheader("場所 vs 日付 (購入金額ヒートマップ)")
                try:
                    pivot1 = pd.pivot_table(df, index='location', columns='date', values='price', aggfunc='sum', fill_value=0)
                    st.dataframe(pivot1.style.background_gradient(cmap="YlOrRd", axis=None))
                except Exception as e:
                    st.info("データが不足しているため表示できません。")

            # 2. 年月別
            with pivot_tab2:
                st.subheader("年月別 内訳")
                try:
                    pivot2 = pd.pivot_table(df, index=['category', 'item_name'], columns='year_month', values='price', aggfunc='sum', fill_value=0)
                    st.dataframe(pivot2)
                except:
                    st.info("データ不足")

            # 3. 日付別
            with pivot_tab3:
                st.subheader("日付別 内訳")
                try:
                    pivot3 = pd.pivot_table(df, index=['category', 'item_name'], columns='date', values='price', aggfunc='sum', fill_value=0)
                    st.dataframe(pivot3)
                except:
                    st.info("データ不足")
            
            # 4. 場所別
            with pivot_tab4:
                st.subheader("場所別 内訳")
                try:
                    pivot4 = pd.pivot_table(df, index=['category', 'item_name'], columns='location', values='price', aggfunc='sum', fill_value=0)
                    st.dataframe(pivot4)
                except:
                    st.info("データ不足")

        else:
            st.info("データがまだありません。「データ登録」タブからレシートを追加してください。")

    # タブ3: データ管理
    with tab3:
        st.header("保存済みデータ一覧")
        df = load_data()
        st.dataframe(df, use_container_width=True)
        
        if not df.empty:
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="CSVダウンロード",
                data=csv,
                file_name=f"kakeibo_data_{datetime.now().strftime('%Y%m%d')}.csv",
                mime='text/csv',
            )

if __name__ == "__main__":
    main()
