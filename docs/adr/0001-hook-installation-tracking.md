# 0001. 用狀態檔取代路徑字串比對來辨識自己裝的 hook

## 狀態

已接受

## 背景

`installer.py` 把自己寫入 `settings.json` 的 hook 條目，跟使用者自己的其他 hook 條目分開管理，好讓 `merge`（安裝/升級）跟 `remove`（卸載）都只動自己的條目。

v0.1.0 的判斷方式（`_is_ours`）是檢查 hook 的 `command` 字串裡有沒有包含 `"claude-code-notify"` 這個子字串。這在唯一文件化的預設安裝路徑（`~/.claude/claude-code-notify/`）下永遠成立，但有兩個結構性問題：

1. **跟 `base_dir` 脫鉤**：只要安裝路徑（`CLAUDE_NOTIFY_HOME`，目前僅測試使用、未對外文件化）改變，新舊條目用的是完全不同的判斷邏輯依據（純粹字串巧合），重裝會產生重複 hook，卸載會留下清不掉的失效條目。這正是 [todo.md](../todo.md) 問題 7。
2. **子字串比對本身不精確**：理論上任何使用者自訂的 hook，只要指令路徑剛好包含這個子字串（例如使用者自己的專案也叫這個名字），就會被誤判為「我們裝的」而被覆蓋或清除。

單純把判斷邏輯從「子字串包含」改成「命令路徑是否以目前的 `base_dir` 為前綴」，可以讓判斷更精確，但沒有解決第 1 點：如果兩次呼叫（例如先裝在路徑 A，之後改用路徑 B 重裝或卸載）用的 `base_dir` 不同，新的呼叫依然無從得知路徑 A 底下的舊條目是自己裝的。

## 決策

`installer.py` 改用一個獨立的狀態檔記錄「上一次實際寫入 `settings.json` 的完整 hook 指令字串」，取代任何依賴路徑內容的猜測：

- 狀態檔命名為 `.claude-code-notify-hooks.json`，**與 `settings.json` 放在同一個目錄**（而不是放在 `base_dir` 底下）。原因：`base_dir`（`CLAUDE_NOTIFY_HOME`）在使用情境上被允許改變（這正是問題 7 的觸發條件），但 `settings.json` 的路徑（`CLAUDE_SETTINGS`）對真實使用者而言幾乎恆定不變，也是測試沙箱化時本來就會跟 `base_dir` 一起被覆寫的參照點（見 `tests/test_install_e2e.py` 的 `_run` helper）。把狀態檔綁在 `settings.json` 旁邊，才能讓它在 `base_dir` 改變後依然被找到。
- `merge_hooks(settings, base_dir, state)` 用狀態檔裡記錄的「上次寫入的確切指令字串」去比對、移除 `settings.json` 裡的舊條目（而不是猜路徑），再寫入指向目前 `base_dir` 的新條目，並回傳更新後的狀態。
- `remove_hooks(settings, state)` 同樣用狀態檔裡記錄的確切指令字串移除對應條目，呼叫端在成功後刪除狀態檔。
- **舊版遷移**：如果某個事件在狀態檔裡沒有記錄（例如使用者從 v0.1.0 舊版升級，尚未產生過狀態檔），才退回使用舊版的子字串比對法，一次性「認領」既有條目，避免升級後產生重複 hook。這個退回路徑只在缺乏狀態記錄時觸發，之後的每次呼叫都會有精確紀錄可用。

## 後果

**正面：**
- 重裝、卸載都不再依賴目前呼叫時的 `base_dir` 剛好跟過去一致；只要 `settings.json` 本身沒變，安裝路徑無論怎麼改，狀態都追蹤得到。
- 不再有「使用者自己的 hook 剛好命令路徑含有這個子字串」而被誤判的風險（比對改成精確字串相等）。
- 對現有（v0.1.0）安裝的使用者透明：第一次升級時自動遷移，不需要使用者手動介入。

**代價：**
- 在 `~/.claude/`（跟 `settings.json` 同一層目錄，而非本工具專屬的 `~/.claude/claude-code-notify/`）多放了一個小檔案。這跟「設定檔案集中在自己的目錄」的既有慣例（見 [產品文件](../claude-notify-product-doc.md) §5.3）有一點點出入，但這個狀態檔不含任何機密資訊，且卸載時會一併清除。
- `installer.py` 的 `merge_hooks`/`remove_hooks` 函式簽章改變（多了 `state` 參數、`merge_hooks` 回傳值變成 tuple），屬於內部 API，呼叫端（`main()`）已同步更新，對 `install.sh` 的外部呼叫介面（CLI 參數）無影響。
- 仍然只保護「`settings.json` 路徑不變、`base_dir` 改變」的情境。如果使用者連 `CLAUDE_SETTINGS` 也改了且沒有搬移狀態檔，一樣會失去追蹤——但這已經超出 v1「global-install-only」的文件化使用情境。

## 相關

- [todo.md](../todo.md) 問題 7
- [claude-notify-product-doc.md](../claude-notify-product-doc.md) §5.4（hook 整合設計）
