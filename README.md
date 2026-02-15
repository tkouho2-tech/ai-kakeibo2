# AI家計簿アプリ (Streamlit)

AIを活用してレシートを解析し、家計簿データとして蓄積・可視化するアプリケーションです。

## 1. 準備

### 必要なライブラリのインストール
以下のコマンドを実行して、必要なライブラリをインストールしてください。

```bash
pip install -r requirements.txt
```

### Gemini APIキーの設定
このアプリは Google Gemini API を使用します。
APIキーを取得していない場合は [Google AI Studio](https://aistudio.google.com/app/apikey) から取得してください。

#### 方法 A: `secrets.toml` を作成する (推奨)
1. `.streamlit` フォルダを作成し、その中に `secrets.toml` というファイルを作成します。
2. 以下のようにAPIキーを記述します。

```toml
# .streamlit/secrets.toml
GEMINI_API_KEY = "あなたのAPIキーをここに貼り付け"
```

#### 方法 B: 環境変数で設定する (一時的)
Windows (PowerShell):
```powershell
$env:GEMINI_API_KEY="あなたのAPIキー"
streamlit run app.py
```

## 2. 起動方法

以下のコマンドでアプリを起動します。

```bash
streamlit run app.py
```

ブラウザが自動的に立ち上がり、アプリが表示されます。

## 3. デプロイ (Streamlit Community Cloud)

このアプリをインターネット上で公開するには、Streamlit Community Cloud が便利です。

### 手順

1. このリポジトリを GitHub にプッシュします。
2. [Streamlit Community Cloud](https://streamlit.io/cloud) にサインアップ/ログインします。
3. "New app" をクリックし、このリポジトリを選択します。
4. "Advanced settings" をクリックし、Secrets 欄に以下のように API キーを設定します。

```toml
GEMINI_API_KEY = "あなたのAPIキー"
```

5. "Deploy!" をクリックします。

### ⚠️ 注意事項 (SQLiteについて)

このアプリはデータベースとして SQLite (`*.db` ファイル) を使用しています。
Streamlit Community Cloud の仕様上、**アプリが再起動またはスリープすると、保存されたデータは初期化（リセット）されます。**

永続的にデータを保存したい場合は、Google Sheets や外部データベース (PostgreSQL, Supabase など) との連携が必要です。
