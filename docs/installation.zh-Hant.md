# 安裝與啟用指南

[繁體中文](installation.zh-Hant.md) · [English](installation.en.md) · [返回產品介紹](../README.md) · [版本更新](UPDATES.md)

> **適用版本：0.3.0。** 藍圖工作流採用 [Apache 2.0](../LICENSE) 開源授權。原始碼、安裝器及合成測試位於 [`plugins/blueprint-workflow`](../plugins/blueprint-workflow)，可直接從本倉庫安裝，毋須訂閱藍圖工作室服務。模型服務本身的存取及用量條件仍由供應商決定。

## 1. 安裝前準備

請先準備以下環境：

- **Python 3.12 或以上版本**，並確認可在終端機執行。
- 已安裝並可使用的目標 AI Agent：Codex、Claude Code、Cursor 或 Gemini CLI。
- 已安裝及登入的背景分析引擎：Codex CLI、Claude Code 或 Gemini CLI。
- Git，以及可寫入本機私人資料目錄的權限。

前台 Agent 負責與你合作完成工作；背景引擎負責持續整理工作流，兩者可以分開選擇。Cursor 必須另外明確選擇受支援的背景引擎。

外掛沿用所選 CLI 的現有登入，不會代你登入或複製帳戶憑證。模型服務的使用權限與用量限制，仍以所選供應商為準。

## 2. 取得原始碼

在終端機複製公開倉庫，再進入**包含 `scripts` 資料夾的外掛目錄**：

```powershell
git clone https://github.com/collieai-g/Blueprint-Workflow.git
cd Blueprint-Workflow/plugins/blueprint-workflow
python --version
```

以下安裝命令均在 `plugins/blueprint-workflow` 內執行。GitHub 的原始碼 ZIP 亦可使用；解壓後須進入相同的外掛子目錄。

## 3. 選擇一個目標 Agent

每次執行與目標 Agent 對應的一行命令。

| 目標 Agent | Windows 安裝命令 |
| --- | --- |
| Codex | `& .\scripts\install-local.ps1 -Host codex` |
| Claude Code | `& .\scripts\install-local.ps1 -Host claude-code` |
| Cursor，使用 Codex 背景引擎 | `& .\scripts\install-local.ps1 -Host cursor -Provider codex` |
| Gemini CLI | `& .\scripts\install-local.ps1 -Host gemini-cli` |

`-Provider` 可指定 `codex`、`claude-code` 或 `gemini-cli`；`-Model` 可選擇該引擎可用的模型識別名稱。選擇其他背景引擎前，請先完成該 CLI 的安裝與登入。

安裝器會建立外部私人 Python 環境並安裝所需依賴，為目標宿主準備對應接駁。不會自動改用另一間模型供應商。

### 先查看計劃或準備檔案

```powershell
# 只顯示安裝計劃，不寫入安裝設定
& .\scripts\install-local.ps1 -Host codex -DryRun

# 準備接駁套件，不完成宿主註冊
& .\scripts\install-local.ps1 -Host codex -StageOnly
```

準備完成、註冊完成、工具已載入及實際分析成功，是不同的核對項目。

### macOS 與 Linux

套件亦提供共用命令列安裝入口，例如：

```sh
sh scripts/install-local.sh --host codex
sh scripts/install-local.sh --host cursor --provider codex
```

這些系統的原生端到端驗收仍須在對應環境完成；提供安裝入口並不代表所有平台均已完成實測。

## 4. 核對宿主與記錄方式

| 宿主 | 0.3.0 記錄方式與啟用要求 |
| --- | --- |
| Codex | 提供工作範圍內的原生 hooks 接駁；須在 **Settings → Hooks** 或 `/hooks` 檢視並信任，再核對實際記錄。 |
| Claude Code | 提供原生工作 hooks 接駁；須符合宿主支援、信任及權限要求。 |
| Cursor | 由前台 AI 透過 MCP 保存語意檢查點；本版未啟用自動 hooks。 |
| Gemini CLI | 由前台 AI 透過 MCP 保存語意檢查點；本版未啟用自動 hooks。 |

可在套件根目錄檢查本機接駁狀態：

```powershell
python scripts/launch.py doctor --host codex
```

將 `codex` 換成你的目標宿主。檢查結果可協助確認安裝檔案及設定，但不會呼叫模型，也不等同背景登入、記錄完整性或重用測試已通過。

常見狀態包括：

- `staged`：套件已準備，宿主註冊尚未完成。
- `installed_local`：已完成本機註冊或安裝接駁檔案；仍須在宿主內核對工具是否可用。
- `analysis_configuration_required`：需要明確選擇受支援的背景引擎。
- `waiting_for_worker`：既有分析尚未結束，待背景程序平順停止後再執行安裝。

0.3.0 已完成 Windows 合成資料及 Codex 實際背景分析、受控研究驗收。其他宿主的原生載入、真實登入與模型請求，仍須逐項核對；不能由安裝成功推定所有能力均已驗證。

## 5. 開始第一個工作流

在已啟用外掛的原對話中，說明你的實際目標與完成標準：

> 使用藍圖工作流記錄今次任務，一邊工作一邊整理可重用流程。目標是……完成標準是……

之後照常與 AI 工作、提供資料及提出修正。每次工作由當次需求建立方法，並在同一原對話內接續。背景會逐步整理草稿；值得討論的改善會在合適工作節點提出，由你決定是否採納。

完成實際工作並核對成品後，再明確要求定稿：

> 今次工作已經 100% 完成，幫我製作可重用工作流。

定稿代表方法已整理至可測試狀態。換用新輸入後，仍須實際執行及核對當次成果，才能記錄該次重用通過。

## 6. 本機資料、更新與移除

Windows 預設私人資料根目錄為 `%LOCALAPPDATA%\BlueprintWorkflowPlugin`。記錄、草稿、工作流版本與匯出資料，與公開原始碼分開保存。程式在本機運行，AI 推理仍使用你選擇的模型服務；本機保存不等於離線推理，請依實際工作授權提供資料。

帳戶內已安裝的外掛、Git marketplace 安裝及本機安裝，使用不同更新流程。請先按[版本更新指南](UPDATES.md)識別來源；不能只靠 GitHub 版本號推定已安裝副本更新。

對從本機原始碼安裝的接駁，先取得所選版本的原始碼，再在外掛目錄執行相同安裝命令。安裝器會先要求既有背景程序平順停止，並在需要遷移既有資料時建立備份；不要以刪除私人資料目錄的方式更新。宿主載入新版後，仍須核對版本、工具及記錄能力。

如需移除某一宿主的本機接駁，可於外掛原始碼目錄執行：

```powershell
python scripts/launch.py uninstall --host codex
```

移除宿主接駁會保留私人工作記錄、版本及匯出資料；刪除私人資料是另一項操作。

---

[返回產品介紹](../README.md) · [English installation guide](installation.en.md)
