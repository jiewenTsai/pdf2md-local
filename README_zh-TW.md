# pdf2md-local

在自己的電腦上，把書籍或論文的 PDF 轉成 Markdown。把 PDF 拉進 **input_pdf**，幾分鐘後轉好的 md 就出現在 **output_md**：
- 公式轉成 LaTeX；
- 表格保留結構；
- 圖片另存成圖檔，放在 md 旁邊。

```
input_pdf/                       output_md/
  paper.pdf   ──監看程式──►        okafor_2021_variational-inference-rating/
  done/paper.pdf                     okafor_2021_variational-inference-rating.md
                                     page_3_image_1.jpg ...
```

轉檔用的是 [MinerU](https://github.com/opendatalab/MinerU)，在本機的 GPU 上執行。支援 Windows、macOS（Apple Silicon）和 Linux，不會上傳任何東西。

它絕對不會：
- 覆蓋已經存在的 md；
- 刪除 PDF（轉好的 PDF 會移到 `input_pdf/done/`）；
- 把 PDF 放進 output 資料夾；
- 用 `--remote` 執行 MinerU。

---

## 一、最低需求

pdf2md-local 一定要有 GPU。安裝程式第一步就會檢查，電腦不符合需求時，在下載任何東西之前就會停下來。

| 項目 | 需求 |
|---|---|
| GPU（三選一） | 獨立顯卡 NVIDIA／AMD，顯示記憶體 6 GB 以上（Windows、Linux）<br>Apple Silicon，macOS 14 以上<br>內顯 Intel Iris Xe／Arc 或 AMD Radeon 680M–890M（最低要 Iris Xe 等級） |
| 記憶體 | 獨立顯卡或 Apple Silicon：8 GB；內顯：16 GB（內顯和系統共用記憶體） |
| 硬碟 | 剩餘空間 15 GB |
| Python | MinerU 需要 3.11–3.14（安裝程式會自己找） |
| Linux | 需要 Vulkan：`sudo apt install libvulkan1 mesa-vulkan-drivers` |

**不支援：**
- 只有 CPU；
- Intel UHD 7xx：能轉但太慢，每頁約 142 秒，30 頁的論文約 70 分鐘；
- Intel HD／UHD 6xx；
- 6 GB 以下的獨立顯卡；
- Intel 處理器的 Mac。

**實測 6 頁的速度：**

| 硬體 | 時間 |
|---|---|
| RTX 3060 | 54 秒 |
| Intel UHD 770 | 851 秒（結果和 RTX 3060 相同） |
| 只用 CPU | 估計約 10 小時（測到一半就停了） |

`--accept-gpu` 可以讓程式不認得的 GPU 通過檢查，但只有 CPU 的電腦無論如何都不會通過。

## 二、安裝

在終端機貼上一行就好（會把這個 repo 下載到暫存資料夾，再執行安裝程式）：

Windows（PowerShell）：

```powershell
irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1 | iex
```

macOS、Linux：

```sh
curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh
```

想先只檢查硬體：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.ps1))) -Check
```

```sh
curl -fsSL https://raw.githubusercontent.com/jiewenTsai/pdf2md-local/main/install.sh | sh -s -- --check
```

也可以下載或 clone 這個資料夾，在終端機進入資料夾後執行：

| 系統 | 指令 |
|---|---|
| macOS、Linux | `sh install.sh` |
| Windows | `powershell -ExecutionPolicy Bypass -File install.ps1` |

只想檢查硬體時，加上 `--check`（Windows 用 `-Check`），這樣不會安裝任何東西。

安裝程式分五步：
1. 檢查硬體。
2. 找 Python 3.11–3.14。
3. 把 MinerU 4.2.3 裝到 `~/.pdf2md-local/envs/mineru`。電腦上已經有名叫 `pdf2md-mineru` 的 conda 環境時，直接沿用，不會升級它。
4. 把程式複製到 `~/.pdf2md-local/app`，並確認 MinerU 看得到 GPU。
5. 建立 `~/pdf2md-local/input_pdf` 和 `~/pdf2md-local/output_md`，啟動背景監看程式，並加入 `pdf2md-local` 指令。

第一次轉檔時，MinerU 要先下載模型（幾 GB），所以只有第一次會比較慢。

**移除**：執行 `sh install.sh --uninstall`（Windows 用 `-Uninstall`）。這會停止監看程式，並刪除 `~/.pdf2md-local`。你的兩個資料夾和裡面的檔案都會保留。

## 三、使用方式

| 你想要 | 這樣做 |
|---|---|
| 轉一份 PDF | 把它拉進 `input_pdf` |
| 打開兩個資料夾 | `pdf2md-local open` |
| 看目前狀況 | `pdf2md-local status`：監看程式狀態、正在轉的檔案與進度、等待中的 PDF、最近的紀錄 |
| 馬上轉，並顯示進度條 | `pdf2md-local run` |
| 原地轉某份 PDF（不移動它） | `pdf2md-local convert paper.pdf` |
| 查某份 PDF 轉過了沒 | `pdf2md-local check paper.pdf` |
| 檢查安裝狀況 | `pdf2md-local doctor` |

監看程式只轉 60 秒內沒有變動的檔案，所以還在複製中的 PDF 不會被拿去轉。轉完的 PDF 會依結果移到下面其中一個資料夾：
- `done/`：轉好了。
- `skipped/`：已經有 md，沒有重轉。
- `failed/`：轉檔失敗，原因寫在 input 資料夾裡的 `pdf2md-local_log.txt`。

## 四、資料夾可以隨時搬動或改名

`input_pdf` 和 `output_md` 可以隨時搬到家目錄底下的任何地方，例如桌面、文件或 Dropbox 資料夾，也可以改名。

每個資料夾裡都有一個隱藏檔 `.pdf2md-local_folder.json`，裡面存著一組隨機編號。**這個檔案請不要刪。**

資料夾不在原位時，pdf2md-local 會：
- 在家目錄底下找這組編號，最多往下 5 層，隱藏資料夾和系統資料夾不找；
- 找到後改用新位置，並更新設定檔；
- 如果監看程式正在執行，約 15 秒內就會跟上。

它不會在舊位置重新建立資料夾。找不到時（例如搬到別的磁碟），指令會告訴你找不到；這時執行 `pdf2md-local setup` 指定新位置即可。

## 五、命名規則

每個 md 資料夾都命名為 `第一作者姓_年份_短標題`，全部小寫。名稱依下面的順序決定，前面的方法找不到才用下一個：
1. PDF 裡的 DOI，到 Crossref 查；
2. arXiv 編號，到 arXiv 查；
3. md 的標題，到 Crossref 搜尋；
4. 離線時，從 md 第一頁讀出標題、第一作者和年份；
5. 以上都失敗時，用檔名。

Zotero 格式的檔名（`作者 - 2021 - 標題.pdf`）直接沿用。

查詢時只會把 DOI、arXiv 編號或標題送到 api.crossref.org 或 export.arxiv.org，PDF 本身不會離開電腦。想完全離線，在設定檔裡設 `"metadata_lookup": false`。

`output_md/pdf2md-local_index.json` 記錄每份 PDF 的 SHA-256 對應到哪個資料夾。同一篇論文換了檔名再丟進來，也不會重轉。

每份 md 的最前面有 YAML front matter：
- `source_pdf`、`tool`、`tool_version`、`converted`；
- `title`、`doi`、`named_from`、`source_sha256`。

## 六、背景監看程式與設定檔

監看程式在各系統上的執行方式：

| 系統 | 方式 |
|---|---|
| Windows | 工作排程器的工作 `pdf2md-local watcher`：登入時啟動，另外每 5 分鐘補啟動一次（已在執行時會略過），不會跳出視窗 |
| macOS | launchd agent `~/Library/LaunchAgents/com.pdf2md-local.watcher.plist`（KeepAlive） |
| Linux | systemd 使用者服務 `pdf2md-local-watcher.service`（Restart=always）。想在登出後繼續執行：`loginctl enable-linger $USER` |

設定檔位置：

| 系統 | 路徑 |
|---|---|
| Windows | `%APPDATA%\pdf2md-local\config.json` |
| macOS | `~/Library/Application Support/pdf2md-local/config.json` |
| Linux | `~/.config/pdf2md-local/config.json` |

資料夾設定的優先順序：
1. 指令參數 `--input`、`--output`；
2. 環境變數 `PDF2MD_LOCAL_INPUT`、`PDF2MD_LOCAL_OUTPUT`。用這種方式指定的資料夾照原樣使用，搬動後不會自動跟上；
3. 設定檔。

另外，`PDF2MD_LOCAL_CONFIG` 可以指定改用另一個設定檔。

## 七、疑難排解

| 狀況 | 處理 |
|---|---|
| 安裝時說硬體不符合 | 執行 `pdf2md-local hardware`（或 `python3 pdf2md_local.py hardware`）看原因；確定 GPU 夠力但程式不認得時，加上 `--accept-gpu` |
| Ubuntu 上 MinerU 看不到 GPU | `sudo apt install libvulkan1 mesa-vulkan-drivers`，再執行 `pdf2md-local doctor` 確認 |
| 丟進去的 PDF 沒動靜 | `pdf2md-local status` 看監看程式有沒有在執行；沒有的話執行 `pdf2md-local service install` |
| 找不到資料夾 | `pdf2md-local setup` 重新指定位置 |
| 在終端機裡找不到 `pdf2md-local` 指令 | 開一個新的終端機。Linux／macOS 要確認 `~/.local/bin` 在 PATH 裡 |

## 八、目前狀態

已在 Windows 11（RTX 3060）上測試過。macOS 和 Linux 的程式都寫好了，單元測試也涵蓋了各平台的邏輯，但還沒有在這兩個系統上實際跑過。

## 開發

```
python -m unittest discover -s tests
```

測試只用 Python 標準函式庫；不管有沒有安裝 `psutil` 和 `watchdog` 都能跑。

## 授權

MIT。MinerU 本身另有授權條款，請見它的 repo。
