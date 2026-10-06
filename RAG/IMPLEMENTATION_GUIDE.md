# 実装エージェント向け指示書

## あなたの役割

このプロジェクトの実装担当エージェントとして、隣接する設計資料と既存コードを読み、Sync処理とQuery処理を分離したローカルRAGアプリを保守してください。設計の要約だけで終えず、必要なコード、テスト、起動手順まで完成させてください。既存ファイルがある場合は先に読み、利用者の変更を保持してください。

## 1. 最初に読む資料

1. `docs/system-design.md`: 目的、前提、アーキテクチャ、制約。
2. `docs/detailed-design.md`: 設定、データ形式、処理順、API、テスト仕様。
3. `docs/class-design.md`: 責務と依存境界。

資料間で矛盾する場合は、実装を始める前に矛盾箇所を報告し、根拠なしに仕様を拡張しないでください。現在の環境で確認できた事実を推測より優先してください。

## 2. 実装対象

Windows上で動作する、ローカルPDFと任意のGoogle Driveフォルダーを同期する日本語RAGアプリを保守してください。

- 質問処理と同期処理を分離する。同期はアプリ内のバックグラウンドSync Workerが行い、Queryは同期と独立して動く。
- 初回同期で検索可能な資料がなければ質問を拒否する。ready資料がある状態の更新同期中は旧版で検索を継続する。
- `documents/`内のPDFと、設定された場合のGoogle Drive PDF/Google Docs/Google Slidesを対象にする。Webアップロードは実装しない。
- Vector SearchとSQLite FTS5/BM25をRRFで融合し、設定時はCross-Encoderで再順位付けする。
- 回答に検索根拠のファイル名、ページ、抜粋を示す。Drive文書には検証済みfile ID由来のURLを付ける。
- 根拠が見つからない場合はLLMを呼ばず、指定文言で回答を保留する。Context上限と参照ID検証を守る。
- 同期はファイル単位で失敗を報告する。旧ready版があるファイルの更新失敗では旧版を保つ。
- 画像のみのPDFへのOCR、外部公開、ユーザー認証、複数ユーザー、サーバー側会話履歴は今回の範囲外。

## 3. 環境・依存関係

添付された依存一覧を基準とし、次の直接依存を優先してください。

```text
Flask==3.1.3
pypdfium2==5.13.0
qdrant-client==1.19.0
sentence-transformers==5.7.0
langchain-text-splitters==1.1.2
ollama==0.6.2
python-dotenv==1.2.3
google-api-python-client==2.201.0
google-auth-oauthlib==1.5.0
```

1. 実装・検証環境はPython `3.10.11`（Windows）。変更後はこの環境でテストし、READMEと実環境の差異を明示してください。
2. `requirements.txt` にはアプリが直接使う依存だけを記載し、添付にある無関係な全パッケージや推移依存を丸ごと転記しないでください。
3. 直接依存は `requirements.txt` にあるものを使い、追加パッケージが本当に必要な場合は用途を設計資料へ反映してください。無断でメジャーアップグレードしないでください。
4. 「gema3.12」は `gemma3:12b` と解釈し、モデルタグは設定で変更可能にしてください。`ollama list` で実在するタグを確認し、未取得ならREADMEに `ollama pull gemma3:12b` を案内してください。
5. Embeddingは `intfloat/multilingual-e5-base` を既定とし、文書には `passage: `、質問には `query: ` を必ず付けてください。モデルの初回取得にネットワークが必要になる可能性を説明してください。別モデルへ無言で切り替えないでください。

## 4. 実装順序

### Phase A: 環境と骨格

- リポジトリの既存状態を確認する。既存の設計資料・ユーザー変更を上書きしない。
- 既存の`rag_app/application/query`、`application/sync`、`infrastructure/{sources,parsing,ml,storage}`の境界を維持する。
- Domainと`ports/interfaces.py`は外部SDK型を持たず、アプリケーションサービスはProtocol越しに依存する。
- Qdrant、`data/sync_state.sqlite3`、`data/keyword_index.sqlite3`の用途を混ぜない。
- `.env`、OAuth token、PDF原本、QdrantとSQLiteデータをGit対象外にする。
- dotenvはプロジェクトルートの`.env`を明示して読み、カレントディレクトリに依存しない。

### Phase B: バックグラウンド同期

- Sync Workerから`DocumentIngestionService`を呼び、質問API thread内では同期しない。
- `LocalFolderSource`と任意の`GoogleDriveSource`は、本文取得前に文書revisionを列挙する。Drive Changes tokenは全ファイル同期成功時だけcommitする。
- `pypdfium2`でページ単位抽出し、700文字/overlap 100文字のChunkerでページをまたがず分割する。
- revisionとSHA-256を使って変更ファイルだけをEmbeddingする。破損・暗号化・画像PDFなどはファイル単位で失敗として記録し、残りは継続する。
- 新版ポイントをQdrant/FTS5にpending登録し、SQLiteのready切替後に公開する。更新失敗では旧ready版を保つ。
- 同期元一覧の完全取得後に削除を反映する。旧JSON manifestは初回に一度だけSQLiteへ移行し、新規状態保存には使わない。

### Phase C: 検索と回答

- `qdrant-client` のLocal永続モードを `data/qdrant/` で既定使用する。`QDRANT_URL` がある場合のみサーバーモードへ切り替える。
- コレクションがない場合だけ作成する。モデル次元・distance不一致で既存データを自動削除しない。
- Qdrant Vector SearchとSQLite FTS5/BM25の結果をRRFで融合し、`RETRIEVAL_CANDIDATES`件からready/hash/model/pathの一致する候補だけを残す。
- 関連度判定後、設定されている場合はCross-Encoderで再順位付けする。`TOP_K`件、`MAX_CONTEXT_CHARS`以内の根拠をPromptへ渡す。
- 根拠なし・低関連度時はOllamaを呼ばない。Contextに実際に含めた資料だけを出典にし、未知の参照IDを除去する。
- UIへ回答、ファイル名、ページ番号、抜粋、Drive URLを返す。PDF内の指示文を信頼しないようsystem promptへ明記する。

### Phase D: WebとCLI

- `/` に日本語の質問画面を用意する。
- `/api/chat`、`/api/health`に加え、`/api/sync/status`、`/api/sync`、`/api/drive/connect`を提供する。
- 初回に検索可能な資料がないときだけ質問を拒否し、旧ready版がある更新同期中はQueryを継続する。
- UIに送信中、入力エラー、依存サービス停止、初回同期状態、進捗、失敗一覧、出典表示を実装する。
- JSで回答・PDF名をDOMへ設定するときは `textContent` を使う。HTML文字列として信頼しない。
- CLIは`ingest`、`drive-sync`、`rebuild-index --confirm`を提供する。Qdrant LocalではWebアプリを停止して実行する。
- Flaskはdebug/reloaderを無効にし、`127.0.0.1`で単一プロセス・複数request threadとして起動する。WorkerとQueryは共有アダプターのロックを使う。

### Phase E: テストとドキュメント

- 外部Ollama、Qdrantサーバー、モデルダウンロードなしで動く `unittest` を実装する。外部依存はport越しにFake/Mockへ置換する。
- READMEと3設計書を現在の実装に合わせ、Windows PowerShellのセットアップ、OAuth設定、モデル取得、同期、起動、テスト、制限、再構築方法を記載する。
- 実環境で確認していない統合テストを成功したと報告しない。

## 5. 必須設定の初期値

```dotenv
APP_HOST=127.0.0.1
APP_PORT=5000
DOCUMENTS_DIR=documents
GOOGLE_DRIVE_FOLDER_ID=
GOOGLE_CLIENT_SECRETS_PATH=secrets/google_client_secret.json
GOOGLE_TOKEN_PATH=secrets/google_token.json
QDRANT_PATH=data/qdrant
QDRANT_URL=
QDRANT_COLLECTION=pdf_knowledge_v1
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=gemma3:12b
EMBEDDING_MODEL=intfloat/multilingual-e5-base
CHUNK_SIZE=700
CHUNK_OVERLAP=100
TOP_K=5
RETRIEVAL_CANDIDATES=20
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
SYNC_INTERVAL_SECONDS=300
MIN_RELEVANCE_SCORE=0.45
MAX_CONTEXT_CHARS=10000
MAX_QUESTION_CHARS=2000
OLLAMA_TIMEOUT_SECONDS=360
```

`MIN_RELEVANCE_SCORE=0.45` は初期値にすぎません。scoreはモデル・文書に依存するため、閾値を評価質問で調整するようREADMEに明記してください。値をコードへ散在させず設定として扱ってください。

## 6. 必須受け入れ条件

- [ ] `documents/`のPDFと、設定されていればGoogle Drive対象フォルダーの文書を同期できる。
- [ ] Sync Workerは質問処理と別に動き、初回同期前は質問を拒否し、既存ready版がある更新同期中は検索を続ける。
- [ ] ページ番号が1始まりで、PDFビューアのページと一致する。
- [ ] 空PDF、画像のみPDF、破損PDF、暗号化PDF、権限エラーを無言で成功扱いせず、ファイル単位で報告する。
- [ ] revision/SHA-256が変わらない文書は再Embeddingせず、更新版はpending登録とready切替を経て公開する。
- [ ] 更新失敗では旧ready版を保持し、同期元から削除された文書はQdrant・FTS5・SQLite状態から除去する。
- [ ] Vector/BM25のRRF、ready/hash照合、閾値棄却、Reranker、Context上限を通した回答を検証する。
- [ ] Qdrant既存コレクションの次元不一致時に、データを自動消去せず明示エラーになる。
- [ ] 資料に答えがある質問で、回答・ファイル名・ページ番号・抜粋を返し、Drive文書には元ファイルへのリンクを付ける。
- [ ] 資料に答えがない質問、関連度不足、ready条件を満たさない資料だけが候補の質問で固定の根拠不足回答を返し、Ollamaを呼ばない。
- [ ] PDF中の命令文をシステム命令として実行しないプロンプトとテストがある。
- [ ] 空質問・長すぎる質問を拒否し、エラー時に内部パスやスタックトレースをUIへ漏らさない。
- [ ] `python -m unittest discover -s tests -v` が外部サービスなしで成功する。
- [ ] `python -m rag_app.cli ingest`、`drive-sync`、`rebuild-index --confirm`、`python app.py`の実行方法がREADMEと一致する。
- [ ] READMEに、実文書評価のRecall@5・出典正確性・回答妥当性・根拠なし質問の棄却手順と初期目標を記載する。評価未実施の状態で精度を保証しない。

## 7. 推奨確認コマンド

Windows PowerShellで実行する。環境に存在しない確認対象がある場合は、その事実を報告する。

```powershell
python --version
python -m pip show Flask pypdfium2 qdrant-client sentence-transformers langchain-text-splitters ollama python-dotenv google-api-python-client google-auth-oauthlib
python -m unittest discover -s tests -v
python -m compileall -q rag_app app.py
python -m rag_app.cli ingest
python app.py
```

Ollamaの統合確認では、Ollamaが起動し `gemma3:12b` が利用可能であることを確認する。Qdrant Localを利用する場合は索引CLI終了後にWebアプリを起動する。

## 8. 完了時の報告

完了報告には、実装した主要機能、作成した起動コマンド、実際に成功したテスト、実行できなかった統合確認とその理由、実データを使うために利用者が行う操作（PDF配置、Ollamaモデル取得など）を簡潔に記載してください。未検証の精度を保証したり、未実行のコマンドを成功と記載したりしないでください。
