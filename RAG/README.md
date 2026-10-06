# 書類ナレッジ

ローカルのPDFとGoogle Driveの資料をバックグラウンドで同期し、日本語の質問に根拠付きで回答するRAGアプリです。同期Workerと質問処理は別々に動きます。初回索引中は質問を受け付けず、既存の資料がある場合は更新同期中も検索を続けます。Web画面からPDFをアップロードする機能はありません。

## 構成

```text
app.py
rag_app/
	application/
		query/       Hybrid検索、関連度判定、Rerank、Context、回答
		sync/        差分取り込み、バックグラウンドWorker
	domain/        文書・チャンク・同期状態・例外
	infrastructure/
		sources/     ローカルフォルダー、Google Drive、OAuth
		parsing/     PDF抽出、ページ単位Chunking
		ml/          E5 Embedding、Cross-Encoder、Ollama
		storage/     Qdrant、SQLite FTS5、SQLite同期状態
	ports/         アプリケーションとアダプター間の契約
	web/           Flask画面とAPI
tests/            外部サービスを使わないテスト
data/
	qdrant/                検索用のベクトルDB
	keyword_index.sqlite3  BM25キーワード索引
	sync_state.sqlite3     ファイル版・同期状態・変更トークン
```

Qdrantは検索用DB、SQLiteは同期管理DBとキーワード検索索引です。旧版の`data/index_manifest.json`は初回起動時にSQLiteへ一度だけ取り込みます。

## 環境とセットアップ

実装環境はPython `3.10.11`、Windowsです。PowerShellでプロジェクトルートに移動し、仮想環境を作成します。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

PowerShellの実行ポリシーによりActivateが拒否される場合は、`.\.venv\Scripts\python.exe`を直接使えます。`.env`はプロジェクトルートから明示的に読み込みます。Webサーバーは`127.0.0.1`だけで待ち受けます。

Ollamaを起動し、回答モデルを取得します。

```powershell
ollama list
ollama pull gemma3:12b
```

Embeddingは`intfloat/multilingual-e5-base`、Rerankerは`BAAI/bge-reranker-v2-m3`を既定で使います。初回起動では両モデルをHugging Faceから取得するため、ネットワークと十分なメモリが必要です。Rerankerを無効にする場合は`.env`で`RERANKER_MODEL=`を空にします。モデルの暗黙の切替はしません。文書Embeddingに`passage: `、質問Embeddingに`query: `を付けます。

## 同期元

**ローカルPDF:** テキストPDFを`documents/`へ配置します。サブフォルダーも対象です。画像だけのPDFはOCRせず、ファイル単位で失敗として報告します。

**Google Drive:** Google CloudでDrive APIを有効にし、デスクトップアプリ用OAuthクライアントJSONを`secrets/google_client_secret.json`へ配置します。`.env`の`GOOGLE_DRIVE_FOLDER_ID`を同期したいフォルダーIDへ変更してください。アプリ画面の「Google Driveに接続」からブラウザーでアクセスを許可すると、トークンを`secrets/google_token.json`へ保存します。指定フォルダーとその配下のPDF、Google Docs、Google Slidesを対象にします。

## 起動と動作

```powershell
python app.py
```

`http://127.0.0.1:5000`を開きます。起動時に同期Workerが動き、以後`SYNC_INTERVAL_SECONDS`（既定300秒）ごとに差分を確認します。画面の「同期」で今すぐ同期を要求できます。Workerが同期しても、readyな資料がある間はその旧版で検索できます。初回同期で検索可能な資料がまだない場合は、完了するまで質問欄を無効化します。

変更検知ではローカルPDFの更新日時・サイズ、Drive PDFのMD5、Google形式ファイルの更新日時を使います。変更があったファイルだけ本文を取得し、SHA-256確認後にParse、ページ内Chunking、Embeddingを行います。新版のポイントは検索対象外で準備し、SQLiteのready状態へ切り替えた後に公開します。失敗した更新は旧ready版を維持します。削除が確認できた資料はQdrant・キーワード索引・同期状態から除去します。

質問処理ではE5ベクトル検索とSQLite FTS5 trigramのキーワード検索（固有名詞・型番向け）を行い、Reciprocal Rank Fusionで候補を統合します。既定ではCross-Encoderが候補を再順位付けし、上位`TOP_K=5`件を文字数上限内でLLMへ渡します。ready状態・本文ハッシュが一致する出典だけを回答に添え、Google Drive由来の資料は元ファイルへのリンクを表示します。根拠がないと判断した場合は資料から確認できない旨を返します。

主なAPIは`POST /api/chat`、`GET /api/health`、`GET /api/sync/status`、`POST /api/sync`、`POST /api/drive/connect`です。

## 管理コマンド

通常運用ではCLIの索引登録は不要です。障害調査などで手動同期する場合は、Qdrant Localのロックを避けるためWebアプリを停止してから実行します。

```powershell
python -m rag_app.cli ingest
python -m rag_app.cli drive-sync
```

全件再構築はQdrantコレクションとSQLite同期状態を削除する破壊的操作です。必要な場合のみ明示確認して実行してください。

```powershell
python -m rag_app.cli rebuild-index --confirm
```

Qdrantの次元またはCosine distanceが不一致でも、起動時に既存コレクションを自動削除しません。再構築する場合は上記の確認付きコマンドを使います。

## テストと評価

```powershell
python -m unittest discover -s tests -v
python -m compileall -q rag_app app.py
```

テストは外部Ollama、Qdrant Server、モデルダウンロード、実PDFを必要としません。実文書の品質は保証しません。導入後、最低20件の評価質問について正解可能/不能、期待要点、正しいファイルとページを記録し、Retrieval Recall@5、出典正確性、回答妥当性、根拠なし質問の棄却を別々に評価してください。`MIN_RELEVANCE_SCORE=0.45`は初期値で、モデル・文書に依存します。閾値は評価セットで調整してください。

| 指標 | 初期目標 |
|---|---:|
| Retrieval Recall@5 | 90%以上 |
| 出典正確性 | 誤ったファイル・ページを出典にしない |
| 回答妥当性 | 期待要点を満たす回答が90%以上 |
| 根拠なし質問の棄却 | 根拠のない断定回答が0件 |

## 制限

- OCR、PDFアップロード、外部公開、複数ユーザー、サーバー側会話履歴には対応しません。
- OAuth認可はこのローカルユーザーのブラウザーで行います。Google Driveへのアクセス権はOAuth設定に従います。
- 質問、PDF本文、Embedding、回答はローカル処理です。初回モデル取得とGoogle Drive同期では外部通信が発生します。
- Qdrant Localは単一プロセスで使用してください。手動CLI実行時はWebアプリを停止します。
- `.env`、OAuthトークン、PDF原本、Qdrant、SQLiteデータはGit対象外です。