# Wish Builder

[English](README.md) | 繁體中文

把一句產品想法，變成經過審閱、可以追蹤，也能讓 Agent 接手推進的軟體專案。

Wish Builder 是一套給 Codex 使用的 Skill，適合那些不能只靠一句提示就直接開始寫程式的專案。你先說明大方向，[gstack](https://github.com/garrytan/gstack) 協助整理產品與工程方案，由人確認產品範圍和架構；之後再由 [Trellis](https://github.com/mindfold-ai/Trellis) 建立可編輯的任務與依賴。Wish Builder 讀取並檢查任務圖，鎖定批准的版本，再監督後續執行。

它不會另外建立第二套任務拆分器或任務資料庫。可編輯的任務圖屬於 Trellis；批准後的執行快照，以及約束 Agent 的規則，屬於 Wish Builder。

> **目前狀態：** 開發預覽版（`0.1.0.dev1`）。本機控制流程、不可變執行快照檢查、遇到不確定情況就停止的准入規則、Journal 與恢復邊界、Git adapter，以及 Wish Builder 對官方 Trellis `0.6.15` 的匯入／投影 bridge 都已有實作。包含 Git 變更途中當機情況的完整本機生命週期，已使用受控 subprocess worker 通過端到端測試。
>
> `Codex / Windows` 已完成本地資格驗證並正式發布，可執行真實派工，最大並行度為 2。
> 記錄涵蓋完整 turn、執行中取消、當機重啟後不重送的 reconcile、cleanup，以及兩個
> owned paths 互不重疊的 sibling 同時執行。證據和套件完整性經獨立核對後，才由
> fail-closed 發布器加入精確版本記錄和編譯 registry trust pin。這是採用人工接受之 detached
> provider provenance 的本地正式發布，不是 OpenAI 簽署的 attestation，也不是 OpenAI
> 官方認證。Pi、Oh My Pi 和 Codex/Linux 仍是 candidate，不能派工；未知、candidate、
> quarantined 或不相符的 backend 版本都會直接停止。官方 Trellis `0.6.15` 也沒有跨程序 compare-and-swap
> （CAS），所以投影採單一寫入者並在衝突時停止。Agent 派工和 Trellis 投影是分開的：
> worker 只寫隔離 Git worktree 和 Journal，之後由單一 writer 把結果投影回 Trellis。
> Repository 已公開，並以 GPL-3.0-only 授權發布
> [`v0.1.0.dev1`](https://github.com/chriskcl/wish-builder/releases/tag/v0.1.0.dev1) 預覽版。

## 為什麼需要它

一句簡短的產品想法很適合當起點，卻不足以安全地安排多個開發 Agent。沒有清楚流程時，常見情況是需求還沒定好就開始寫程式、幾個 Agent 同時改到相同檔案，或做到一半才發現架構需要重來。

Wish Builder 把工作維持在固定順序：

1. 修改產品程式碼之前，先把產品方向說清楚。
2. 由人批准範圍與架構。
3. 由 Trellis 把批准的文件整理成小任務和明確依賴。
4. 派工之前，先驗證並鎖定一個確切的任務圖版本。
5. 讓互不衝突的工作平行進行，再按固定順序測試、審閱和合併。

目的不是把人完全拿掉，而是把人的注意力留給產品和架構決定，範圍清楚的工程工作才交給 Agent。

## 運作方式

```text
┌────────────────────────────────────────┐
│ 你提出產品方向                         │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ gstack 在獨立子工作階段審閱            │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Wish Builder 集中整理重要決定          │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Gate A：批准產品範圍與架構             │
└───────────────┬────────────────────────┘
         通過   │       要改 → 退回 gstack
                v
┌────────────────────────────────────────┐
│ Trellis 建立任務與依賴                 │<────┐
└───────────────────┬────────────────────┘     │
                    │                          │
                    v                          │
┌────────────────────────────────────────┐     │
│ Wish Builder 匯入並驗證                │─────┘
└───────────────────┬────────────────────┘  有問題 → 退回 Trellis
                    │
                    v
┌────────────────────────────────────────┐
│ Gate B：批准任務圖投影與衍生摘要       │
└───────────────┬────────────────────────┘
         通過   │       要改 → 退回 Trellis
                v
┌────────────────────────────────────────┐
│ 鎖定、派工、驗證與恢復                 │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ 測試、審閱、合併與歸檔                 │
└────────────────────────────────────────┘
```

這是完整流程的目標設計。目前預覽版已包含組裝完成的本機生命週期與當機恢復路徑，並使用受控 subprocess worker 通過驗證。Trellis 相容性已通過匯入和單一寫入者投影檢查。另一份 backend version registry 只准入 `Codex 0.149.0 / Windows`，並行度可為 1 或 2；其他隨附版本都仍是不可派工的 candidate。

規劃階段通常依序使用 `office-hours`、`plan-ceo-review`、`plan-eng-review`；產品有畫面時，再加上 `plan-design-review`。每個 review 都在獨立、非互動的子工作階段執行。子工作階段只暫用 review 明確標示的推薦選項，讓審閱可以完成，再交回實際結果、其他選擇、日後是否容易修改和技術原因。純工程且容易撤回的選擇可以自動記錄；產品、架構、成本、安全或其他重要決定，會改寫成白話後集中放進 Gate A。gstack 的推薦只是建議，不代表人已批准。子工作階段若直接向使用者提問，或交回的決策資料不完整，該次 review 會停止。

Gate A 是由人批准「要做什麼」和「主要部分如何配合」的決定點。

接著由 Trellis 準備候選任務圖。Wish Builder 檢查依賴、負責檔案、驗收指令和任務大小。發現問題就退回 Trellis 修改，不會自己另建一份任務清單。檢查時使用的任務圖快照、revision 摘要和 manifest 都是 Wish Builder 衍生契約，不是 Trellis 官方 API。

Gate B 批准從一次穩定 Trellis task-record 讀取投影出的 material graph，以及 Wish Builder 衍生的任務圖和 execution manifest 摘要、scheduler、worker backend 和權限。Wish Builder 衍生的 task-record revision digest 只作 provenance；只要 canonical graph digest 不變，status、progress 和其他生命週期變動不會讓 Gate B 失效。Trellis 任務圖只要有實質改動，舊批准就會失效。Gate B 通過前，Wish Builder 不會修改產品程式碼，也不會派出實作 Agent。

## Trellis 與 Wish Builder 的分工

| Trellis 負責 | Wish Builder 負責 |
| --- | --- |
| 建立與拆分任務 | 驗證匯入的任務圖 |
| 編輯任務依賴 | Gate B 與內容摘要 |
| 任務內容和生命週期 | 不可變的執行快照 |
| Implement、Check、Finish | 准入、fencing、Journal 和恢復 |
| 任務歷史與歸檔 | 結果驗證與合併准入 |

這個專案不包含 PRD-to-task 生成器、任務 CRUD、另一個看板或第二套任務資料庫。

## 排程與 Agent backend

整體設計定義了兩種互斥的排程模式：

| `scheduler_mode` | `worker_backend` | 預定分工 | 目前 M1 狀態 |
| --- | --- | --- | --- |
| `trellis` | `trellis` | Trellis 排程同層任務；Wish Builder 驗證和監督 | 關閉：Trellis scheduler 尚未有通過資格驗證的派工前准入與 fencing 整合；`0.6.15` 也沒有跨程序 CAS |
| `wish_builder` | `pi`、`oh_my_pi` 或 `codex` | Wish Builder 按凍結任務圖派工到隔離 worktree；另一個單一 writer 稍後把 Journal 結果投影到 Trellis | 精確的 `Codex 0.149.0 / Windows` 已在本地取得並行度 1-2 資格；其他隨附版本都是 candidate |

M1 目前的 Python 控制層只接受 `scheduler_mode=wish_builder`。每次執行只選一種 backend。啟動前，Wish Builder 會探測已安裝套件，要求精確版本、npm integrity、protocol profile、launch profile、OS 和並行度都符合固定 registry。未知、candidate、quarantined 或已 drift 的版本會直接停止，不會猜格式、偷偷降級或換成另一種。`Codex 0.149.0 / Windows` 可在並行度 1 或 2 通過准入；並行度 3 回傳 `concurrency_not_qualified`，其他隨附版本則會在啟動 Agent 前回傳 `dispatch_not_qualified`。

未來實作 Trellis scheduler 時，`GraphIndex` 仍只會是驗證和恢復索引，不會變成第二個 dispatcher。

目前對 backend 的判定刻意保守：

| Backend | Windows 證據 | Linux 證據 | 正式派工 |
| --- | --- | --- | --- |
| Codex | `0.149.0` 已取得資格；最多 2 個並行 turn | `0.149.0` 是 candidate；仍需完整真實資格驗證 | 只開放 Windows `0.149.0` |
| Pi | `0.84.2` 是 candidate；只有啟動和 handshake，沒有 model turn | `0.84.2` 是 candidate；仍需完整真實資格驗證 | 關閉 |
| Oh My Pi | `17.4.0` 是 candidate；需要已設定的 model 和 credential | `17.4.0` 是 candidate；仍需完整真實資格驗證 | 關閉 |

本地已發布的 `Codex 0.149.0 / Windows` 記錄完成 full turn、active cancellation、crash/reconcile、
cleanup、parallel overlap 和平台證據，現在是 `status=qualified`，且
`maxConcurrency=2`。其來源 revision 為
`fd3296ed1f8d85e9a1347eb1e2dcdf611ec62720`。獨立核對亦確認官方
`@openai/codex@0.149.0` 主套件和 Windows native package 的 npm integrity 與本機安裝
檔案一致。保存的 provenance 是人工接受的本地 detached provider reference，不是 OpenAI
簽署的 attestation。其餘五個隨附版本記錄仍是 `status=candidate`。

Trellis 相容性和 backend 資格是不同記錄。Trellis 記錄綁定凍結任務圖與 projection
adapter；穩定 backend baseline 保存 policy、capability、launch profile 和歷史證據；backend
version registry 則決定某個精確 backend／OS／version 能否派工。active `wish_builder` 派工
必須有符合已批准 baseline profile 和 launch digest 的 qualified 版本；因 worker 不寫 Trellis，所以不把 projection CAS
當成派工條件。未來由 Trellis 排程的模式不使用 Agent backend／OS cell，但要有新版
manifest schema、派工前准入、fencing、stop/reject 和並行寫入所有權資格。Claude Code 和
macOS 已延後，等前三個 backend 與 Windows／Linux 矩陣穩定後再處理。

Trellis 相容性和 backend 派工資格是兩份不同契約：

- [`wish_builder/compatibility/trellis-0.6.15.json`](wish_builder/compatibility/trellis-0.6.15.json) 驗證官方 `@mindfoldhq/trellis@0.6.15` 與 `@mindfoldhq/trellis-core@0.6.15`，只涵蓋文件所述的匯入和單一寫入者投影邊界。
- [`wish_builder/compatibility/backend-qualification-0.6.15.json`](wish_builder/compatibility/backend-qualification-0.6.15.json) 是穩定的 adapter policy、capability、launch profile 與歷史證據 baseline。
- [`wish_builder/compatibility/backend-version-registry.json`](wish_builder/compatibility/backend-version-registry.json) 是精確 backend／OS／version 的派工權威；目前只把 `Codex 0.149.0 / Windows` 標為 qualified，並行度上限為 2。

官方 Trellis `0.6.15` 沒有可靠的跨程序 CAS；M1 因此同一時間只允許一個投影寫入者，只接受穩定的 task record 讀取，寫入前核對預期 SHA-256，寫入後再驗證 SHA-256 和內容，遇到衝突或結果不明就停止。這些 digest 檢查是投影完整性保護，不是 CAS，也不是 Agent 派工鎖。backend worker 只寫隔離 Git worktree 和 Journal。另一個 Trellis scheduler 模式還需要通過資格驗證的派工前准入、fencing 和並行寫入所有權。

## 已實作內容

目前 repository 包含：

- 嚴格的輸入格式和固定、可重現的錯誤訊息；
- 從官方 Trellis `0.6.15` task record 衍生確定性的 Wish Builder 任務圖快照，再產生 manifest v2；
- 透過官方 Core `loadTaskRecord`／`writeTaskRecord`，把單一寫入者的生命週期結果投影到權威 Trellis repository，並執行穩定讀取、寫前／寫後摘要檢查；寫前摘要衝突後不會自動重試；
- 依賴、負責檔案、ready set 和需求追蹤檢查；
- 與內容摘要綁定的 Gate 決定，後續修改會讓舊批准失效；
- 可重複安全執行的 Gate B 正式寫入；即使 Journal 已有後續執行事件，也不會破壞或重寫它們；
- append-only Journal、lease、epoch、fencing、checkpoint、replay 和 `GraphIndex` 重建；
- 具備可續期 scheduler lease 的前景總控、隔離的 attempt worktree、固定 promotion 順序，
  以及使用模擬 subprocess 的端到端測試；
- 重啟後能辨認已完成的 Codex turn、保留 worktree 身份，並且只在舊 worker 已確定結束時恢復；
- 目標分支前進前，會在實際 promotion candidate 裡執行一般專案的驗收指令；
- subprocess 隔離、輸出限制、timeout 與 fail-closed 恢復；
- Git staging、promotion、cleanup、quarantine 和 trace/export service；
- protocol profile adapter，以及 fail-closed 的精確版本探測與准入；
- 獨立核對 backend 證據後，以 fail-closed 流程發布 candidate、qualified 或 quarantined 版本記錄，並保存 evidence digest 和編譯 registry trust pin；
- Python package 與獨立 Skill runtime 同步，以及可重現的開發版 ZIP；
- contracts、排程、恢復、Git effects、打包和受控效能的本機測試。

這些元件已有實作和測試。組裝完成的本機生命週期，包括 Git 變更途中當機後的恢復，已使用受控 subprocess worker 通過端到端測試。受保護的 `wishctl run` 入口會先探測 SDK，再只准入本地已發布的精確 `Codex 0.149.0 / Windows` 記錄，並行度可為 1 或 2。剩餘的 backend 工作，是在把其他 candidate 升為 qualified 前，產出並獨立核對同樣完整、可按內容摘要核對的資格證據。

真實 Issue、Pull Request、託管平台、憑證、background supervisor 和正式部署 adapter 都不在目前實作內。

## 安裝開發預覽版

### 使用前準備

- Python 3.11 或更新版本
- Git
- Node.js 18.17 或更新版本
- 支援 Skill 的 Codex
- gstack
- `@mindfoldhq/trellis@0.6.15`
- 供 bridge 使用、已驗證的本機 `@mindfoldhq/trellis-core@0.6.15` SDK

Python runtime 本身不依賴第三方 package。Wish Builder 會在開始前列出缺少的工具；未經批准，它不會自行安裝全域工具、登入帳號或修改 repository 設定。

Trellis CLI 必須按確切版本安裝：

```bash
npm install -g @mindfoldhq/trellis@0.6.15
```

Core bridge 可讀取解壓後的 `@mindfoldhq/trellis-core@0.6.15` package 目錄，或經過驗證的官方 npm tarball。tarball 只是本機驗證輸入，不會被打包進 Wish Builder 發行內容。不可改用 `@latest` 或其他預發布版本。

### 安裝 Skill ZIP

從已發布的預覽版下載 [`wish-builder-skill-0.1.0.dev1.zip`](https://github.com/chriskcl/wish-builder/releases/download/v0.1.0.dev1/wish-builder-skill-0.1.0.dev1.zip) 和 [`SHA256SUMS`](https://github.com/chriskcl/wish-builder/releases/download/v0.1.0.dev1/SHA256SUMS)。Repository 內也保留了已同步的 [`wish-builder-skill.zip`](wish-builder-skill.zip)，方便直接從原始碼 checkout 測試。

已標記的 `v0.1.0.dev1` 資產早於本頁所述的 `Unreleased` backend version registry 變更。下一個預覽版發布前，如要測試目前 `main` 的行為，請使用同一 source revision 內的 repository ZIP。

Windows PowerShell：

```powershell
Expand-Archive .\wish-builder-skill-0.1.0.dev1.zip -DestinationPath "$env:USERPROFILE\.codex\skills"
```

macOS 或 Linux：

```bash
mkdir -p ~/.codex/skills
unzip wish-builder-skill-0.1.0.dev1.zip -d ~/.codex/skills
```

安裝後應該看到：

```text
~/.codex/skills/wish-builder/SKILL.md
```

Repository 內 ZIP 的 SHA-256（預發布下載檔請以該版本的 `SHA256SUMS` 為準）：

```text
adcda3a2a2aaa26785e3def244a45a37d5df2e9d506c72b374ea86d7fd6bd58f
```

Repository 已公開，也可以透過 Codex Skill installer 直接從 GitHub 安裝其中的 `wish-builder/` 目錄。

## 開始一個專案

在 Codex 開啟目標 Git repository，再給 Wish Builder 一個簡短方向：

```text
Use $wish-builder in this repository.

我想做一個給兩位室友使用的共同記帳工具。
第一版只在本機使用，不需要付款或銀行連接。
我批准產品、架構和任務圖後，你可以自行繼續。
只有工作超出批准範圍、需要高風險操作，
或準備正式部署時才停下來問我。
```

你不用一開始就寫完整規格。早期審閱會逐步找出使用者、問題、成功標準、範圍邊界和架構。
目前預覽版可以把已批准的 Gate B 快照正式寫入，並以前景程序執行通過資格檢查的 manifest。
它不是無人看管的背景服務：執行時要讓指令保持連線；若程序中斷，對同一份 manifest 重跑指令，
就會進入受保護的恢復流程。

## 仍需要人決定的地方

| 決定點 | 何時出現 | 你要批准什麼 |
| --- | --- | --- |
| Setup gate | 需要工具、登入或 repository 變更時 | 安裝、初始化、認證或設定 |
| Gate A | Trellis 準備任務圖前 | 產品目標、範圍、架構、資料與安全選擇 |
| Gate B | 派出實作 Agent 前 | 從 Trellis task records 投影出的 material graph、Wish Builder 衍生的任務圖與 manifest 摘要、scheduler、backend、測試、合併規則和權限 |
| Gate C | 正式部署前 | 部署位置、風險、檢查和回復方案 |
| 偏離決定 | 工作超出批准邊界時 | 是否修改範圍、架構、公開介面或安全邊界 |

願望不等於批准。Gate A 和 Gate B 都需要明確回覆「通過」，或列出要改的內容。

## 安全邊界

未取得額外批准時，Wish Builder 不會：

- 索取、保存或更換憑證；
- 花錢或修改帳單；
- 部署到正式環境；
- 刪除正式資料或降低存取控制；
- 執行無法回復的資料轉換；
- 改變已批准的產品方向、架構或公開介面；
- 讓 Trellis 和 Wish Builder 同時派發同一個任務圖。

一般的實作選擇和普通測試失敗由 coordinator 處理。範圍變更、高風險操作和無法排除的重複失敗，才會退回給人決定。

## 指令列檢查

`wishctl` 把重要流程規則變成可重複執行的檢查。執行時只使用 Python 標準函式庫。

| 指令 | 用途 |
| --- | --- |
| `validate` | 驗證批准、任務圖、檔案範圍、測試與恢復資料 |
| `ready` | 列出凍結任務圖中可開始且互不衝突的任務 |
| `drift` | 檢查修改檔案是否落在任務負責範圍內 |
| `trace` | 產生需求、任務和交付結果的追蹤文件 |
| `hash` | 計算 Gate 文件的 SHA-256 |
| `snapshot-trellis` | 從官方 Trellis `0.6.15` task record 衍生 Wish Builder 任務圖快照 |
| `import-trellis` | 把 Wish Builder 衍生的 Trellis 任務圖快照轉成 manifest v2 |
| `admit-gate-b` | 核對並正式記錄已批准的 Gate B 快照，之後才允許執行 |
| `run` | 在前景執行已准入且通過資格檢查的 manifest，重啟後走安全恢復流程 |
| `backend-probe` | 不啟動 backend，檢查已安裝套件的精確版本、integrity、profile、OS 狀態與並行度上限 |
| `decide` | 把 direct CLI Gate 決定寫入 Journal |
| `resume` | 根據驗證過的恢復證明，恢復一項狀態不明的派工 |

範例：

```bash
python scripts/wishctl.py --help
python scripts/wishctl.py backend-probe --provider codex --provider-sdk-root C:/path/to/pinned-sdk-root
python scripts/wishctl.py validate path/to/execution-manifest.json --stage planning
python scripts/wishctl.py snapshot-trellis <parent-task-id> --core-archive path/to/mindfoldhq-trellis-core-0.6.15.tgz --output trellis-graph.json
python scripts/wishctl.py import-trellis path/to/trellis-graph.json path/to/import-settings.json --output execution-manifest.json
python scripts/wishctl.py admit-gate-b execution-manifest.json gate-b-<sha256>.md import-settings.json --approved-artifact-hash <sha256> --runtime-root path/to/run --workspace-root . --actor-id <actor>
python scripts/wishctl.py run execution-manifest.json --runtime-root path/to/run --workspace-root . --provider-sdk-root C:/path/to/pinned-sdk-root --core-root C:/path/to/trellis-core
```

安裝後的 Skill 也提供相同 runtime：`wish-builder/scripts/wishctl.py`。`backend-probe` 只有在
精確版本已 qualified 時回傳 exit code `0`；未知、candidate、quarantined 或 drift 回傳 `1`，
輸入格式錯誤回傳 `2`。

維護者可在不修改 execution kernel 的情況下更新 registry：

```powershell
python scripts\manage_backend_versions.py candidate --help
python scripts\manage_backend_versions.py qualify --help
python scripts\manage_backend_versions.py quarantine --help
```

每次更新都要提供當前 registry digest。新探測到的版本先是 `candidate`。只有固定本地 harness
完成派工、structured result、取消、當機恢復、cleanup、sibling overlap、批准並行度和惡意輸入
檢查，再經另一人核對證據，才能升為 `qualified`。有問題的版本可以直接改成 `quarantined`，
不必修改 `TaskDag`、`GraphIndex`、Gate、Journal 或 recovery。

## Repository 結構

```text
.
|-- README.md                     英文說明與使用方式
|-- README.zh-TW.md               繁中說明與使用方式
|-- pyproject.toml                Python package 與 wishctl 入口
|-- wish_builder/                 正式 Python 實作
|   |-- adapters/                 Trellis、process、storage 與 Git 邊界
|   |-- compatibility/            Trellis 相容性、穩定 backend baseline 與精確版本 registry
|   |-- contracts/                輸入與 artifact 格式
|   |-- kernel/                   DAG、Gate、狀態與 GraphIndex
|   |-- presentation/             trace 與 export 輸出
|   |-- processes/                coordinator 與 worker 執行
|   `-- services/                 Journal、恢復、cleanup 與 Git service
|-- wish-builder/                 可獨立安裝的 Codex Skill
|-- scripts/                      build、同步與 CI 檢查
|-- tests/                        unit、integration、fault 與效能測試
`-- wish-builder-skill.zip        可重現的開發版壓縮檔
```

完整執行規則在 [`wish-builder/SKILL.md`](wish-builder/SKILL.md)，artifact 格式和工具邊界則放在 [`wish-builder/references/`](wish-builder/references/)。

## 驗證狀態

**本地測試通過。** 依目前的 M1 規則，這已足以接受這個開發預覽版。

| 檢查 | 結果 |
| --- | --- |
| 較早的本地非效能矩陣 | Windows／Linux × Python 3.11／3.12／3.13；每格執行 1,498 項，0 failure、0 error；Windows 允許略過 9 項，Linux 允許略過 13 項 |
| 最新本地完整測試 | Windows／Python 3.13.14；1,589 項非效能測試加 16 項效能測試，0 failure、0 error，3 項平台條件式略過 |
| Codex/Windows 證據獨立核對 | 52 項通過，1 項因 Windows symlink 權限略過；結論為 `PASS` |
| 發布後資格與准入測試 | 68 項通過，1 項因 Windows symlink 權限略過，另有 59 個 subtests 通過 |
| 官方 Trellis `0.6.15` 整合 | 本次 Windows 通過 24 項 Node bridge 測試與 11 項 Python 整合測試；固定的跨平台證據在各平台保留相同測試集 |
| Skill 打包與已安裝 runtime | 完整套件中的 245 項打包與發布規則測試，以及 13 項 standalone runtime smoke tests 通過 |
| Python 編譯與空白格式檢查 | 通過 |

以上都是本地結果。GitHub Actions 因預算用完而沒有執行，因此本項目不會把候選版本描述成 CI 通過或失敗。`Codex / Windows` 的資格同樣是本地正式發布，不是 provider 官方認證。

本機主要測試指令：

```powershell
uv run --python 3.13.14 --no-project python scripts\ci_test_suite.py --exclude-package performance
uv run --python 3.13.14 --no-project python scripts\ci_test_suite.py --only-package performance
uv run --python 3.13.14 --no-project python wish-builder\scripts\test_wishctl.py
```

若需要更嚴格、可重建的發行資料，仍可選用以下本地工具，把原始結果和發行檔綁定到同一個 commit：

```powershell
uv run --locked --python 3.13 python scripts\local_evidence_packet.py `
  --evidence-root <evidence-root> --candidate-revision <commit-sha> `
  --safety-base-ref <base-ref> --output <manifest.json> `
  --digest-output <manifest.sha256>

uv run --locked --python 3.13 python scripts\ci_local_release.py `
  --repository-root . --evidence-root <evidence-root> `
  --safety-base-ref <base-ref> --distribution-root <distribution-root> `
  --manifest <manifest.json> --manifest-digest <manifest.sha256> `
  --output-dir <release-assets> --revision <commit-sha> `
  --version 0.1.0.dev1 --tag v0.1.0.dev1
```

## 尚餘派工工作

- 把 Pi、Oh My Pi、Codex/Linux 或日後其他版本記錄從 candidate 升為 qualified 前，重跑完整 live qualification 並完成獨立核對。
- 在更強的證據正式發布前，`Codex 0.149.0 / Windows` 的最大並行度維持 2。
- 補上所選流程需要的真實 Issue、Pull Request 和託管平台 adapter。
- 完成一個公開案例，從一句產品願望走到通過審閱並合併的改動。

## 參與開發

提出修改時，先說清楚它解決的實際問題。流程改動應一起更新 Skill、相關參考文件和測試；修改 `wishctl` 時，應加入一個能重現問題的小型測試。

不要在沒有同等替代措施的情況下，移除批准 Gate、負責檔案檢查、fencing 規則或 fail-closed 恢復行為。

## 授權

Wish Builder 採用 [GNU General Public License v3.0 only](LICENSE) 授權。隨附與開發工具的 notices 請見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
