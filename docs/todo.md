# claude-code-notify v0.1.0 — 問題追蹤表

記錄第一版（v0.1.0）以 subagent-driven development 流程實作時，各階段程式碼審查與最終全分支審查中發現的問題。完整執行紀錄另見（本機、未提交）`.superpowers/sdd/progress.md`。

## 問題列表

| 編號 | 發現時間 | 問題描述 | 狀態 | 備註 / 建議 |
|---|---|---|---|---|
| 1 | 2026-07-09（Task 4 審查） | `pending_tracker.load_state` 遇到合法 JSON 但非 dict 型別（如 `null`/`[]`）時會拋出 `AttributeError`，而非依規格回退到全新的 `State()` | ✅ 已處理 | 改為 `except Exception` 廣義攔截；新增測試 `test_wrong_shape_state_falls_back`（commit `1e3d227`） |
| 2 | 2026-07-09（Task 6 審查） | `notifier.send()` 失敗時，雖然頂層錯誤訊息已清洗 token，但透過例外鏈 `__context__` 仍可還原出含 token 的原始 `HTTPError`（`.url` 帶明碼 token） | ✅ 已處理 | 改寫成在 `except` 區塊內先算好清洗後訊息、跳出區塊後再 `raise`，讓 `__context__` 天生為 `None`；新增測試驗證（commit `ef3d732`） |
| 3 | 2026-07-09（Task 7 審查） | `hooks.main()`（真正的行程進入點）讀取 stdin 時沒有 try/except 保護，非 UTF-8 輸入會讓例外未被攔截往外拋，違反「hooks.py 絕不可讓例外中斷使用者對話」的核心規則 | ✅ 已處理 | 包一層 `try/except Exception` 並補上針對 `main()` 本身的測試（commit `c297c64`） |
| 4 | 2026-07-09（Task 10 實作前發現） | 計畫書裡 E2E 測試的 fixture 用 `tmp_path / "ccn"` 當安裝目錄，卻斷言合併後的 hook 指令含有 `"claude-code-notify"` 字串 — 必然測試失敗，實際上正是問題 7 的具體體現 | ✅ 已處理 | 調整測試 fixture 目錄名稱為 `"claude-code-notify"`（測試修正，非產品程式碼變更），未改動 `installer.py` 的標記邏輯本身 |
| 5 | 2026-07-09（最終全分支審查） | `install.sh` 互動式輸入（token / chat id）用裸 `read`，在官方文件推薦的 `curl \| bash` 安裝方式下，stdin 會被 curl 的管道佔用而非使用者鍵盤輸入，導致 `set -e` 中斷安裝 — 新使用者完全無法完成首次安裝 | ✅ 已處理 | 改為明確從 `/dev/tty` 讀取，並在無 tty 時給出清楚錯誤訊息，導引使用 `--non-interactive`（commit `d0f9eb2`） |
| 6 | 2026-07-09（最終全分支審查） | 預設安裝版本 `VERSION="main"` 抓的是 `main` 分支最新提交，而非 README 宣稱的「pinned latest release」 | ✅ 已處理 | 預設情況下改為透過 GitHub API 解析最新 release tag；明確傳入 `--version <tag>` 時完全略過此解析（commit `d0f9eb2`） |
| 7 | 2026-07-09（Task 9 審查，最終審查裁定延後） | `installer.py` 用「hook 指令路徑是否包含字串 `claude-code-notify`」判斷是否為本工具自己裝的 hook。真實預設安裝路徑永遠符合，但若透過 `CLAUDE_NOTIFY_HOME` 環境變數（目前只在測試中使用、未對外文件化）改成不含此字串的路徑，重新安裝會產生重複 hook 條目，`--uninstall` 也會留下失效條目 | ⏳ 未處理（已追蹤為後續修復項） | 建議改成比對 `base_dir` 路徑前綴，而非字串包含判斷；不影響目前唯一文件化的預設安裝流程 |
| 8 | 2026-07-09（最終全分支審查） | 解析最新 release tag 時若 GitHub API 連不上（離線、被限流），會靜默退回抓 `main` 分支，沒有任何警告訊息 | ⏳ 未處理 | 建議加一行 stderr 提示（例如「無法解析最新版本，改用 main」），純體驗優化，不影響功能 |
| 9 | 2026-07-09（Task 9 實作） | `install.sh` 真正下載 tarball 的分支（`curl \| bash` 實際會走的路徑）目前沒有自動化測試覆蓋，只測了本地已有原始碼（`cp`）的分支 | ⏳ 未處理（刻意排除） | 需要網路才能測試，目前故意不做；未來可用 `file://` 本地 tarball fixture 補測 |
| 10 | 2026-07-09（Task 9 審查） | 產品文件（`docs/claude-notify-product-doc.md` §6.2）寫「`--uninstall` 應先詢問是否刪除 `config.env`」，但計劃書給的 `install.sh` 程式碼與實作都是永遠保留、從不詢問 | ⏳ 未處理 | 文件與計劃書描述不一致，非程式碼缺陷（實作忠實遵照計劃書）；建議之後把文件用語與實際行為對齊 |
| 11 | 2026-07-09（Task 2 審查） | `config.py` 的 `parse_env_file` 不支援 `export KEY=value` 這種寫法；使用者若手動編輯 `config.env` 並加上 `export`，key 會被誤讀成 `"export KEY"` | ⏳ 未處理 | 影響範圍小（安裝程式自己寫的 `config.env` 不會加 `export`），可在 README 加一行說明，或加解析支援 |
| 12 | 2026-07-09（Task 8 審查） | 三個 hook shim 腳本（`stop.sh`、`stop_failure.sh`、`permission_request.sh`）只有 `stop.sh` 有端對端 subprocess 測試，另外兩個只驗證檔案存在與可執行 | ⏳ 未處理 | 風險低（三腳本內容幾乎相同，僅事件名不同），非阻塞項 |
| 13 | 2026-07-09（Task 9 審查） | `installer.py` 讀取損毀（非合法 JSON）的 `settings.json` 時會拋出未攔截的 `JSONDecodeError`，導致 `install.sh` 中斷並顯示原始 Python traceback，而非乾淨的錯誤訊息 | ⏳ 未處理 | 屬於少見的第一次安裝失敗情境；可考慮包一層錯誤訊息改善使用者體驗 |

## 圖例

- ✅ 已處理：問題已修復並通過（重新）審查，程式碼已提交
- ⏳ 未處理：已知但刻意延後，不影響目前文件化的預設使用流程（v1 curl | bash 全域安裝）

## 來源

本表格內容整理自 v0.1.0 以 `superpowers:subagent-driven-development` 流程執行時，各任務（Task 1–11）的逐項程式碼審查，以及完成所有任務後的最終全分支審查（commit range `17a8861..d0f9eb2`）。
