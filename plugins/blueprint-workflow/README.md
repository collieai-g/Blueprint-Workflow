# 藍圖工作流外掛

全新、無獨立介面的工作流學習外掛。你照常喺 Agent 指示真實工作，外掛保存語意記錄；獨立本機後台一邊理解、一邊修訂可重用工作流。

0.3.0 加強多日工作嘅可靠記錄、長期記憶、分段草稿、研究及提示閉環。Codex、Claude Code、Cursor、Gemini CLI 本機接駁沿用同一核心、私人資料、MCP 工具及可替換分析引擎。

每次啟動都係一個全新工作。工作方法、工具、路線及完成標準只由當次指示與實際結果建立；核心沒有預載行業案例或其他工作嘅設定。同一工作固定綁定原本對話，跨程式重啟及對話壓縮接續。

## 使用

安裝後，在已啟用此外掛的 Agent 聊天輸入：

> 使用藍圖工作流記錄今次任務，一邊工作一邊整理可重用流程。目標是……完成標準是……

之後照常指示 AI 工作及提出糾正。Agent 會在主要階段、判斷、糾正及結果出現時保存語意記錄，後台獨立更新流程草稿。毋須填寫事件表格或建立流程圖。

完成時輸入：

> 今次工作已經 100% 完成，幫我製作可重用工作流。

Agent 先送清記錄。草稿已追平而完成標準已有當次證據時，前台 Agent 核對版本及成品後可直接定稿；有積壓時，後台完成餘下分析。未解關鍵問題會清楚列出。

再次使用時輸入：

> 按照剛才保存的工作流，用呢組新資料執行，並核對最後成品。

每次執行有獨立進度、輸出及驗收記錄。固定版本保存可變輸入、已確認條件分支及完成條件；後續改動另開版本。

## 一路記錄、一路製作

- 錄製與分析狀態獨立。分析引擎失效時仍保存新記錄，恢復後由原已處理位置補分析。
- Hooks 先清洗文字及移除媒體／憑證，再保存私人收件匣並發出回執；背景按順序入庫。`event_append` 仍回傳正式事件 ID。接收、入庫及分析完成是三個不同狀態。
- 長文字分段保存並附來源 ID。截斷、宿主輸入限制或保存失敗會留下缺口狀態，不能把預覽當完整原文。
- 私人資料根目錄下 `tasks/<session_id>/` 保存來源、當前草稿、記錄缺口及明確登記嘅相關檔案版本。`artifact_register` 只處理當次明確指定嘅檔案，不執行檔案或掃描目錄。
- 長期記憶保存要求、決定、被取代規則及未解問題；草稿按穩定章節局部更新。模型只取得有預算限制嘅相關內容，需要舊資料時經程式限定範圍取回。
- 定稿先排清所有已確認接收嘅記錄，再鎖定最後事件。已追平並核對成果嘅草稿直接定稿，不重讀全歷史。

## 研究與重點提示

同步製作優先。背景只在本質問題、反覆失敗或有依據嘅改善機會出現時提出研究／提示，並交代根據、預期改善、代價及未驗證範圍。比較依照當次目標，不預設某種工具或方法一定最好。

`session_updates` 讀取持久提示匣；前台在合適工作節點呈現最重要一至兩項後，才用 `updates_ack` 保存呈報回條。讀到提示不代表已向使用者展示。

`proposal_decide` 以建議內容版本／雜湊及實際用戶指示記錄採納、拒絕或延後。未回答維持未決；採納即時形成有效決定，再由背景更新草稿。舊分析不能覆蓋新決定，舊內容嘅批准亦不能批准已改寫建議。批准與測試通過分開保存。

研究器與純分析器分開，最多同時處理每工作一項研究，共用領取權避免重複調查。研究只取得必要問題及限制，回傳實際打開嘅公開來源；不能執行原工作或修改正式方法。

首個原生研究接駁是 Codex CLI；必須對當次 CLI 版本、工具限制及真實搜尋完成驗收，私人能力標記相符後才啟用。未驗證、引擎不支援或能力改變時，原對話 AI 可用 `research_next`／`research_report` 處理同一研究佇列，不自行轉換服務。背景完成後嘅提示在下次前台活動呈現，未宣稱可喚醒閒置聊天。

明確執行原生研究驗收可用 `python scripts/verify-research.py --data-dir <外部私人資料目錄>`，會用現有 Codex CLI 登入作一次合成公開文件研究；成功才在該資料目錄保存當次 CLI 身份嘅能力標記，不更新宿主或現有工作。更換 CLI 版本／執行檔後需重新驗收。

`session_status` 顯示錄製／分析狀態、保存缺口、待處理量、分析延遲、有效決定、記憶及私人工作目錄。`history_search`／`source_read` 提供同一工作嘅有界來源取回。

## 本機安裝

由 Git checkout 或解壓原始碼安裝，毋須經網站。需要 Python 3.12+；目標 Agent 要另外安裝，背景引擎使用該 CLI 原有登入。Windows 每次選擇一行執行：

```powershell
& .\scripts\install-local.ps1 -Host codex
& .\scripts\install-local.ps1 -Host claude-code
& .\scripts\install-local.ps1 -Host cursor -Provider codex
& .\scripts\install-local.ps1 -Host gemini-cli
```

`-Provider` 可指定 `codex`、`claude-code` 或 `gemini-cli`，`-Model` 可選模型。Cursor 必須明確選擇背景引擎。安裝器建立外部 Python 環境並安裝固定官方 MCP SDK；每個宿主只有薄接駁套件，共用同一份版本化核心。

只查看安裝計劃可加 `-DryRun`，只準備套件可加 `-StageOnly`。準備套件不代表宿主已安裝或工具已連線。完整 CLI、移除及相容狀態見 [本機接駁說明](adapters/README.md)。

安裝來源、執行環境與私人資料預設位於 `%LOCALAPPDATA%\BlueprintWorkflowPlugin`，其他系統使用 XDG 資料目錄，全部與產品原始碼分開。可用 `-DataDirectory` 指定另一位置。升級先要求舊背景程序平順停止，再作增量遷移及備份。解除安裝保留工作記錄、工作流及匯出；不覆寫其他外掛設定。

0.2 → 0.3 是增量遷移：先停止舊 worker、備份既有 SQLite，再新增記錄／分析狀態、記憶、章節、提示及研究資料。固定版本與重用紀錄維持原格式。既有已截斷內容無法補回，會標明實際保存邊界。

Codex 自動 hooks 需要在 **Settings → Hooks** 或 `/hooks` 檢視並信任；其他宿主亦保留原生安裝審核。安裝器不代替使用者同意。未啟用 hooks 時，Agent 仍可使用語意記錄工具，不能聲稱已自動捕捉所有指示或操作。

## 記錄與背景引擎

- 保存指示、目的、操作摘要、工具文字結果、糾正及成品引用。沒有背景錄影、截圖、鍵盤側錄或隱藏推理收集。媒體及常見密鑰在保存邊界剔除或清洗。
- 宿主綁定包含平台名稱，避免相同聊天 ID 跨平台串線。Codex／Claude Code 提供原生 hooks；Cursor／Gemini 首版以 MCP＋使用指示保存語意 checkpoint，原生 payload 轉接器另有測試。
- 每筆記錄先持久保存再回覆。後台合併短批次，使用「上一版草稿＋新增記錄」分析；草稿、修訂及已處理位置一併提交。
- 每個任務保存自己的引擎及模型，共用 worker 按任務路由。後台採獨立 Codex、Claude Code 或 Gemini CLI 推理程序，關閉可執行工作操作的工具及自訂 hooks，接收同一結構並以共同格式核對輸出。
- 不複製登入憑證，缺少引擎時保留記錄並顯示錯誤，不自行轉用另一間服務。程序與資料在本機，推理仍使用所選 AI 服務。
- Cursor 前台接駁與背景引擎分開。Cursor CLI 自身的隔離方式仍待驗證，所以現階段使用明確選擇的其他背景引擎。
- 更換軟件、環境或業務次序是待確認建議；Agent 要先解釋，再記錄使用者實際同意或拒絕。

分析失敗會保留草稿及游標。`analysis_providers` 只檢查入口，不呼叫模型或證明登入有效；使用者選擇後可用 `analysis_configure` 修改未定稿任務的引擎，再用 `analysis_retry` 續做。暫停、程序中斷及模型重試都不會自動重做外部業務操作。

## 產物與驗證

`workflow_export` 提供 Markdown／JSON；`workflow_materialize` 將兩者及輔助程式寫到私人 exports 目錄，回傳路徑與 SHA-256，不自行執行。核心格式跨宿主共用，MCP 本身不會自動取得整段對話或其他工具紀錄。

定稿狀態是 `ready_to_test`。另一組輸入完成執行及全部完成條件核對後，才記為 `passed_agent_reported`；保留失敗、未知與最新執行狀態。

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install "mcp==2.3.0"
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe scripts\launch.py doctor
```

一般測試使用臨時資料／假 CLI 驗證隔離、JSON 協定、跨任務路由、升級、安裝及資料保留；真實模型、原生宿主載入與重用結果分開驗收。`tests/live_acceptance.py` 是明確選用的 Codex 真實分析驗收，使用合成資料，資料目錄必須在原始碼以外。Claude／Gemini 的真實登入及模型請求、Cursor／Gemini 原生應用載入仍須在已安裝該宿主的環境驗收。

`scripts/package.ps1` 產生本機安裝 ZIP 與 SHA-256，排除私人設定、資料及測試環境。來源、測試、打包、安裝、宿主接駁與真實重用各自有獨立結果。

官方依據：[OpenAI Plugins](https://developers.openai.com/plugins/build/plugins)、[Codex 非互動模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[Gemini 設定](https://geminicli.com/docs/reference/configuration/)、[Gemini 政策](https://geminicli.com/docs/reference/policy-engine/)、[Cursor Plugins](https://cursor.com/docs/plugins)、[MCP 架構](https://modelcontextprotocol.io/specification/2026-07-28/architecture)。
