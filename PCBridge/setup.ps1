param([string]$Mode = "")
# PC Bridge - setup + launcher. Started by START.bat

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# ---- кириллица в консоли: UTF-8 + шрифт Consolas (растровый шрифт даёт "???") ----
try {
    Add-Type -ErrorAction Stop -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PcBridgeConsole {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct FONTINFO {
        public uint cbSize; public uint nFont; public short X; public short Y;
        public int FontFamily; public int FontWeight;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FaceName;
    }
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int h);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool GetCurrentConsoleFontEx(IntPtr h, bool max, ref FONTINFO f);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool SetCurrentConsoleFontEx(IntPtr h, bool max, ref FONTINFO f);
    [DllImport("kernel32.dll")] static extern bool SetConsoleOutputCP(uint cp);
    [DllImport("kernel32.dll")] static extern bool SetConsoleCP(uint cp);
    public static void Fix() {
        SetConsoleOutputCP(65001); SetConsoleCP(65001);
        IntPtr h = GetStdHandle(-11);
        FONTINFO f = new FONTINFO(); f.cbSize = (uint)Marshal.SizeOf(typeof(FONTINFO));
        if (!GetCurrentConsoleFontEx(h, false, ref f)) return;
        string n = f.FaceName ?? "";
        if (n.Contains("Consolas") || n.Contains("Lucida") || n.Contains("Cascadia") || n.Contains("Courier")) return;
        f.nFont = 0; f.X = 0; f.Y = (short)(f.Y < 16 ? 16 : f.Y);
        f.FontFamily = 54; f.FontWeight = 400; f.FaceName = "Consolas";
        SetCurrentConsoleFontEx(h, false, ref f);
    }
}
"@
    [PcBridgeConsole]::Fix()
} catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
try { [Console]::InputEncoding  = New-Object System.Text.UTF8Encoding $false } catch {}
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

# ---- запасной режим: транслит латиницей (START_LATIN.bat создаёт latin.flag) ----
$Latin = Test-Path (Join-Path $PSScriptRoot "latin.flag")
$RuChars = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
$LaChars = @("a","b","v","g","d","e","yo","zh","z","i","y","k","l","m","n","o","p","r","s","t","u","f","kh","ts","ch","sh","sch","","y","","e","yu","ya")
function Tr([string]$s) {
    if (-not $Latin -or [string]::IsNullOrEmpty($s)) { return $s }
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $s.ToCharArray()) {
        $i = $RuChars.IndexOf([char]::ToLowerInvariant($ch))
        if ($i -lt 0) { [void]$sb.Append($ch); continue }
        $t = $LaChars[$i]
        if ([char]::IsUpper($ch) -and $t.Length -gt 0) { $t = $t.Substring(0, 1).ToUpper() + $t.Substring(1) }
        [void]$sb.Append($t)
    }
    return $sb.ToString()
}
function W {
    param([Parameter(Position = 0)]$Object = "", [Parameter(Position = 1)][string]$ForegroundColor = "Gray")
    Microsoft.PowerShell.Utility\Write-Host (Tr ([string]$Object)) -ForegroundColor $ForegroundColor
}
function RH([string]$Prompt) { return (Microsoft.PowerShell.Utility\Read-Host (Tr $Prompt)) }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir = Join-Path $Root "tools"
$LogsDir  = Join-Path $Root "logs"
$CfgPath  = Join-Path $Root "config.json"
$PidPath  = Join-Path $LogsDir "pids.json"
$VenvPy   = Join-Path $Root ".venv\Scripts\python.exe"
$ServerPy = Join-Path $Root "server\server.py"
$Ver = "?"
try { $Ver = (Get-Content (Join-Path $Root "VERSION.txt") -Raw -Encoding UTF8).Trim() } catch {}
foreach ($d in @($ToolsDir, $LogsDir, (Join-Path $Root "workspace"))) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Set-Location $Root
try { $Host.UI.RawUI.WindowTitle = "PC Bridge $Ver для Notion" } catch {}

# ------------------------------------------------------------ helpers ------
function Say($t, $c = "Gray") { W $t -ForegroundColor $c }
function Step($t) { W ""; W "==> $t" -ForegroundColor Cyan }
function Ok($t)   { W "  [OK] $t" -ForegroundColor Green }
function Warn($t) { W "  [!] $t" -ForegroundColor Yellow }
function Fail($t) {
    W ""
    W "  [ОШИБКА] $t" -ForegroundColor Red
    W "  Подробности в папке logs. Описание проблем - README.txt, раздел 'Если что-то не работает'." -ForegroundColor Red
    RH "Нажмите Enter, чтобы закрыть"
    exit 1
}
function AskYN($q, [bool]$def) {
    $d = if ($def) { "Y/n" } else { "y/N" }
    while ($true) {
        $a = RH "  $q [$d]"
        if ([string]::IsNullOrWhiteSpace($a)) { return $def }
        $a = $a.Trim().ToLower()
        if (@("y", "yes", "д", "да", "d", "da", "нуы") -contains $a) { return $true }
        if (@("n", "no", "н", "нет", "net", "т", "тщ") -contains $a) { return $false }
        Warn "Введите y (да) или n (нет)"
    }
}
function AskText($q, $def) {
    $a = RH "  $q [$def]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $def }
    return $a.Trim().Trim('"')
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Download($url, $out) {
    Say "  Скачиваю $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -Headers @{ "User-Agent" = "pc-bridge" }
}
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Test-Port($p) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", [int]$p); $c.Close(); return $true } catch { return $false }
}
function Stop-Tree($id, $started, [string]$name) {
    # Stops the process only if it is really ours: same PID AND same start time (old pids.json without a start time: same name).
    # Without this check a stale pids.json could kill an unrelated program that got the same PID.
    if (-not $id) { return $false }
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    $mine = $false
    if ($started) { try { $mine = ([string]$p.StartTime.ToFileTimeUtc() -eq [string]$started) } catch {} }
    else { $mine = ($p.ProcessName -eq $name) }
    if (-not $mine) { return $false }
    # /T = with child processes (background jobs, rojo serve); cmd keeps taskkill messages off the screen
    try { & cmd.exe /c "taskkill /T /F /PID $id >nul 2>&1" } catch {}
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    return $true
}
function Save-Pids($srv, $ngp) {
    $d = [ordered]@{}
    if ($srv) { $d.server = $srv.Id; try { $d.server_start = [string]$srv.StartTime.ToFileTimeUtc() } catch {} }
    if ($ngp) { $d.ngrok = $ngp.Id; try { $d.ngrok_start = [string]$ngp.StartTime.ToFileTimeUtc() } catch {} }
    $d | ConvertTo-Json | Set-Content -Path $PidPath
}
$script:StoppedOld = $false
function Stop-Old {
    if (-not (Test-Path $PidPath)) { return }
    $p = $null
    try { $p = Get-Content $PidPath -Raw | ConvertFrom-Json } catch {}
    if ($p -and $p.supervisor) {
        # 1.4: the supervisor stops its programs itself when it sees logs\stop.flag
        $sp = Get-Process -Id $p.supervisor -ErrorAction SilentlyContinue
        if ($sp -and $sp.ProcessName -like "python*") {
            $flag = Join-Path $LogsDir "stop.flag"
            Set-Content -Path $flag -Value "stop"
            for ($i = 0; $i -lt 24; $i++) { if ($sp.HasExited) { break }; Start-Sleep -Milliseconds 500 }
            Remove-Item $flag -Force -ErrorAction SilentlyContinue
            $script:StoppedOld = $true
        }
    }
    if ($p) {
        if (Stop-Tree $p.supervisor $null ([string]$p.supervisor_name)) { $script:StoppedOld = $true }
        if (Stop-Tree $p.server $p.server_start "python") { $script:StoppedOld = $true }
        if (Stop-Tree $p.ngrok $p.ngrok_start "ngrok") { $script:StoppedOld = $true }
        if (Stop-Tree $p.llm $null "llama-server") { $script:StoppedOld = $true }
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}
function Test-Signed([string]$file, [string]$publisher) {
    # Authenticode check of a downloaded program: valid signature from the expected publisher
    try { $sig = Get-AuthenticodeSignature -FilePath $file } catch { return $false }
    if ($sig.Status -ne "Valid" -or -not $sig.SignerCertificate) { return $false }
    return ($sig.SignerCertificate.Subject -like "*$publisher*")
}
$Arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
function Tail($file, $n = 15) { if (Test-Path $file) { Get-Content $file -Tail $n | ForEach-Object { Say "    $_" DarkGray } } }

if ($Mode -eq "stop") {
    Stop-Old
    if ($script:StoppedOld) { W "PC Bridge остановлен." -ForegroundColor Green } else { W "PC Bridge не запущен." }
    exit 0
}
W ""
W "  ============================================" -ForegroundColor Cyan
W "     PC Bridge $Ver - мост между Notion-агентом и ПК" -ForegroundColor Cyan
W "  ============================================" -ForegroundColor Cyan

Stop-Old

# ------------------------------------------------------------- config ------
$cfg = [ordered]@{}
$haveCfg = Test-Path $CfgPath
if ($haveCfg) {
    try { (Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).psobject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } }
    catch { Warn "config.json повреждён - настроим заново"; $haveCfg = $false; $cfg = [ordered]@{} }
}
function Def($name, [bool]$default) { if ($cfg.Contains($name)) { return [bool]$cfg[$name] } else { return $default } }
function YN($v) { if ($v) { "да" } else { "нет" } }

# ---- one-screen settings menu: arrows + Space (or numbers when keys can't be read) ----
foreach ($kv in @(@("enable_internet", $true), @("enable_blender", $true), @("enable_roblox", $false),
        @("enable_unreal", $false), @("enable_screen", $false), @("enable_input", $false), @("enable_desktop", $true),
        @("enable_shell", $false), @("full_disk", $false), @("read_only", $false), @("notifications", $true),
        @("autostart", $false), @("token_auth", $true))) {
    if (-not $cfg.Contains($kv[0])) { $cfg[$kv[0]] = $kv[1] }
}
if (-not $cfg.Contains("dangerous_commands")) { $cfg.dangerous_commands = "ask" }
if (-not $cfg.Contains("ai_check")) { $cfg.ai_check = "off" }
if (-not $cfg.Contains("ai_model")) { $cfg.ai_model = "3b" }
if (-not $cfg.workspace) { $cfg.workspace = (Join-Path $Root "workspace") }
if (-not $cfg.port) { $cfg.port = 8765 }

function WL([string]$t, [string]$fg = "Gray", [string]$bg = "") {
    if ($bg) { Microsoft.PowerShell.Utility\Write-Host (Tr $t) -ForegroundColor $fg -BackgroundColor $bg }
    else { Microsoft.PowerShell.Utility\Write-Host (Tr $t) -ForegroundColor $fg }
}
function Can-ReadKey {
    try {
        if ([Console]::IsInputRedirected) { return $false }
        $null = [Console]::KeyAvailable
        return $true
    } catch { return $false }
}
function Ask-Workspace {
    Step "Рабочая папка"
    Say "  От этой папки считаются пути агента. Для работы с кодом укажите корень проекта (репозитория)."
    while ($true) {
        $ws = ([string](AskText "Папка" $cfg.workspace)).Trim().Trim('"')
        if ($ws -and (Test-Path -LiteralPath $ws -PathType Container)) { $cfg.workspace = (Resolve-Path -LiteralPath $ws).Path; break }
        Warn "Папка не найдена: $ws"
    }
}
function Ask-Network {
    Step "Сеть и вход"
    $cfg.port = [int](AskText "Локальный порт" $cfg.port)
    Say "  Вход по токену: в адресе нет секрета, токен вводится в Notion отдельно (заголовок Authorization)."
    $cfg.token_auth = AskYN "Вход по токену? (n = старый способ: ключ прямо в ссылке)" ([bool]$cfg.token_auth)
    if (-not $cfg.token_auth) { Warn "Старый способ: ключ виден в адресе и может попасть в журналы и историю. Рекомендуем вход по токену." }
    if ($cfg.secret -and (AskYN "Создать новый секретный ключ (он же токен)? Старое подключение в Notion перестанет работать" $false)) { $cfg.secret = "" }
    Say "  (Домен ngrok спросим после входа в ngrok.)"
}

$ShellOn = { [bool]$cfg.enable_shell }
$MenuItems = @(
    @{ t = "run"; l = ">>  ЗАПУСТИТЬ  <<"; h = "Сохранить настройки и запустить мост. Клавиша S делает то же из любого пункта." },
    @{ t = "head"; l = "МОДУЛИ" },
    @{ t = "bool"; k = "enable_internet"; l = "Интернет"; h = "Поиск, чтение сайтов, скачивание файлов." },
    @{ t = "bool"; k = "enable_blender"; l = "Blender"; h = "Скрипты bpy, создание сцен, рендер." },
    @{ t = "bool"; k = "enable_roblox"; l = "Roblox Studio"; h = "Проекты и скрипты Luau через Rojo." },
    @{ t = "bool"; k = "enable_unreal"; l = "Unreal Engine 5"; h = "Проекты, Python в редакторе (живо), C++, сборка игры." },
    @{ t = "bool"; k = "enable_screen"; l = "Скриншоты"; h = "Весь экран или окно программы, распознавание текста. Без управления мышью." },
    @{ t = "bool"; k = "enable_input"; l = "Мышь и клавиатура"; h = "Клики, перетаскивание, ввод текста, нажатие клавиш." },
    @{ t = "bool"; k = "enable_desktop"; l = "Буфер, файлы, процессы"; h = "Буфер обмена, открытие файлов и ссылок, список процессов, закрытие зависших программ." },
    @{ t = "head"; l = "БЕЗОПАСНОСТЬ" },
    @{ t = "bool"; k = "enable_shell"; l = "Командная строка"; warn = $true
       h = "ПОЛНЫЙ доступ к ПК: любые команды PowerShell, любые файлы, установка программ. Включайте, только если нужно."
       confirm = @("Командная строка = полный доступ к ПК: агент сможет выполнять ЛЮБЫЕ команды,",
                   "читать и удалять любые файлы, ставить программы. Текст с сайтов и из писем",
                   "может подсунуть агенту вредную команду.") },
    @{ t = "choice"; k = "dangerous_commands"; l = "  Опасные команды"; show = $ShellOn
       opts = @(@("ask", "спрашивать (окно Да/Нет)"), @("block", "всегда запрещать"))
       h = "Формат диска, удаление системных папок, отключение антивируса... Обычные команды (сборка, тесты, git) идут без вопросов." },
    @{ t = "choice"; k = "ai"; l = "  ИИ-проверка команд"; show = $ShellOn
       opts = @(@("off", "выкл"), @("1.5b", "модель 1.5B (~1 ГБ, быстрее)"), @("3b", "модель 3B (~2 ГБ, точнее)"))
       get = { if ($cfg.ai_check -eq "on") { [string]$cfg.ai_model } else { "off" } }
       set = { param($v) if ($v -eq "off") { $cfg.ai_check = "off" } else { $cfg.ai_check = "on"; $cfg.ai_model = $v } }
       h = "Локальная модель Qwen2.5-Coder дополнительно смотрит команды. Может только показать окно Да/Нет. Скачивается один раз." },
    @{ t = "bool"; k = "full_disk"; l = "Весь диск"; warn = $true; h = "Файловые инструменты работают не только в рабочей папке."
       confirm = @("Весь диск = агент сможет читать и менять файлы в любой папке, а не только в рабочей.") },
    @{ t = "bool"; k = "read_only"; l = "Всегда только чтение"; h = "Агент смотрит, ищет и делает скриншоты, но ничего не меняет. Временно включается из трея." },
    @{ t = "head"; l = "ПРОЧЕЕ" },
    @{ t = "bool"; k = "notifications"; l = "Уведомления Windows"; h = "Агент подключился, команда отклонена, туннель упал." },
    @{ t = "bool"; k = "autostart"; l = "Автозапуск с Windows"; h = "Мост запускается сам при входе в Windows (значок в трее)." },
    @{ t = "action"; l = "Рабочая папка"; v = { [string]$cfg.workspace }; run = { Ask-Workspace }; h = "От этой папки считаются пути агента." },
    @{ t = "action"; l = "Сеть и вход"; run = { Ask-Network }
       v = { "порт $($cfg.port), " + $(if ($cfg.token_auth -eq $false) { "ключ в ссылке" } else { "по токену" }) }
       h = "Порт, способ входа из Notion, новый секретный ключ." }
)

function Item-Value($it) {
    switch ($it.t) {
        "bool" { if ($cfg[$it.k]) { return "[x]" } else { return "[ ]" } }
        "choice" {
            $cur = if ($it.get) { & $it.get } else { [string]$cfg[$it.k] }
            foreach ($o in $it.opts) { if ($o[0] -eq $cur) { return "< " + $o[1] + " >" } }
            return "< " + $it.opts[0][1] + " >"
        }
        "action" { return (& $it.v) + "  ..." }
    }
    return ""
}
function Visible-Items { return @($MenuItems | Where-Object { -not $_.show -or (& $_.show) }) }
function Draw-Menu($vis, [int]$cur, [string]$title, [bool]$numbered, $deadline) {
    Clear-Host
    WL ""
    WL "  PC Bridge $Ver  -  $title" Cyan
    if ($numbered) { WL "  Номер пункта - изменить,  Enter - запустить,  q - выход" DarkGray }
    else { WL "  Стрелки - выбор   Пробел/Enter - изменить   S - запустить   Esc - выход" DarkGray }
    $n = 0
    for ($i = 0; $i -lt $vis.Count; $i++) {
        $it = $vis[$i]
        if ($it.t -eq "head") { WL ""; WL "  $($it.l)" DarkCyan; continue }
        $n++
        $num = if ($numbered) { "{0,2}. " -f $n } else { "" }
        if ($it.t -eq "run") { $line = "  $num" + $it.l }
        elseif ($it.t -eq "bool") { $line = "  $num" + (Item-Value $it) + " " + $it.l }
        else { $line = "  $num    " + ("{0,-22}" -f $it.l) + " " + (Item-Value $it) }
        $fg = "Gray"
        if ($it.t -eq "run") { $fg = "Green" }
        elseif ($it.t -eq "bool" -and $cfg[$it.k]) { $fg = $(if ($it.warn) { "Yellow" } else { "White" }) }
        elseif ($it.t -eq "bool") { $fg = "DarkGray" }
        if ($i -eq $cur -and -not $numbered) {
            $w = 72
            try { $w = [Math]::Max(40, [Math]::Min(90, $Host.UI.RawUI.WindowSize.Width - 2)) } catch {}
            if ($line.Length -gt $w) { $line = $line.Substring(0, $w - 1) + "~" }
            WL $line.PadRight($w) "Black" "Cyan"
        } else { WL $line $fg }
    }
    WL ""
    if (-not $numbered -and $cur -ge 0) {
        $h = $vis[$cur].h
        if ($h) { WL "  $h" $(if ($vis[$cur].warn) { "Yellow" } else { "DarkGray" }) }
    }
    if ($deadline) {
        WL ""
        $script:CdRow = -1
        try { $script:CdRow = [Console]::CursorTop } catch {}
        WL "  Запуск через $([int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds)) с... (любая клавиша - остаться в меню)" Green
    }
}
function Confirm-Lines($lines, [bool]$keys) {
    WL ""
    foreach ($l in $lines) { WL "  $l" Yellow }
    if (-not $keys) { return (AskYN "Включить?" $false) }
    WL "  Включить? y = да, любая другая клавиша = нет" Yellow
    $k = [Console]::ReadKey($true)
    return (@("y", "Y", "н", "Н", "д", "Д") -contains [string]$k.KeyChar)
}
function Activate-Item($it, [bool]$keys, [int]$dir = 1) {
    switch ($it.t) {
        "run" { return "run" }
        "bool" {
            $new = -not [bool]$cfg[$it.k]
            if ($new -and $it.confirm -and -not (Confirm-Lines $it.confirm $keys)) { return "" }
            $cfg[$it.k] = $new
        }
        "choice" {
            $cur = if ($it.get) { & $it.get } else { [string]$cfg[$it.k] }
            $idx = 0
            for ($j = 0; $j -lt $it.opts.Count; $j++) { if ($it.opts[$j][0] -eq $cur) { $idx = $j } }
            $v = $it.opts[($idx + $dir + $it.opts.Count) % $it.opts.Count][0]
            if ($it.set) { & $it.set $v } else { $cfg[$it.k] = $v }
        }
        "action" { Clear-Host; & $it.run }
    }
    return ""
}
function Show-Menu([string]$title, [int]$countdown) {
    $keys = Can-ReadKey
    $sel = 0
    $deadline = $null
    if ($keys -and $countdown -gt 0) { $deadline = (Get-Date).AddSeconds($countdown) }
    while ($true) {
        $vis = Visible-Items
        $pick = @(for ($i = 0; $i -lt $vis.Count; $i++) { if ($vis[$i].t -ne "head") { $i } })
        if ($sel -ge $pick.Count) { $sel = $pick.Count - 1 }
        if (-not $keys) {
            Draw-Menu $vis -1 $title $true $null
            $a = ([string](RH "  Номер")).Trim().ToLower()
            if ($a -eq "") { return "run" }
            if (@("q", "й", "exit", "выход") -contains $a) { return "quit" }
            $num = 0
            if ([int]::TryParse($a, [ref]$num) -and $num -ge 1 -and $num -le $pick.Count) {
                if ((Activate-Item $vis[$pick[$num - 1]] $false) -eq "run") { return "run" }
            }
            continue
        }
        Draw-Menu $vis $pick[$sel] $title $false $deadline
        if ($deadline) {
            while (-not [Console]::KeyAvailable) {
                $left = [int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds)
                if ($left -le 0) { return "run" }
                if ($script:CdRow -ge 0) {
                    try {
                        [Console]::SetCursorPosition(0, $script:CdRow)
                        WL ("  Запуск через $left с... (любая клавиша - остаться в меню)").PadRight(70) Green
                    } catch {}
                }
                Start-Sleep -Milliseconds 250
            }
            $deadline = $null
            $null = [Console]::ReadKey($true)  # the key only stops the countdown
            continue
        }
        $k = [Console]::ReadKey($true)
        $ch = [string]$k.KeyChar
        $name = [string]$k.Key
        if ($name -eq "UpArrow") { $sel = ($sel - 1 + $pick.Count) % $pick.Count; continue }
        if ($name -eq "DownArrow" -or $name -eq "Tab") { $sel = ($sel + 1) % $pick.Count; continue }
        if ($name -eq "Home" -or $name -eq "PageUp") { $sel = 0; continue }
        if ($name -eq "End" -or $name -eq "PageDown") { $sel = $pick.Count - 1; continue }
        if ($name -eq "Escape") { return "quit" }
        if (@("s", "S", "ы", "Ы") -contains $ch -or $name -eq "F10") { return "run" }
        $it = $vis[$pick[$sel]]
        if ($name -eq "Spacebar" -or $name -eq "Enter" -or $name -eq "RightArrow") {
            if ((Activate-Item $it $true 1) -eq "run") { return "run" }
        } elseif ($name -eq "LeftArrow" -and $it.t -ne "action" -and $it.t -ne "run") {
            $null = Activate-Item $it $true -1
        }
    }
}

$menuTitle = if (-not $haveCfg) { "первая настройка" } elseif ($Mode -eq "reconfigure") { "настройки" } else { "сохранённые настройки" }
$menuWait = if ($haveCfg -and $Mode -ne "reconfigure") { 8 } else { 0 }
$doWizard = (-not $haveCfg) -or ($Mode -eq "reconfigure")
$choice = Show-Menu $menuTitle $menuWait
Clear-Host
if ($choice -eq "quit") {
    W ""
    W "  Выход без запуска: изменения не сохранены, PC Bridge не запущен." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
    exit 0
}
W ""
W "  PC Bridge $Ver - запуск" -ForegroundColor Cyan
if (-not $cfg.secret) { $cfg.secret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N") }
if (-not $cfg.workspace) { $cfg.workspace = (Join-Path $Root "workspace") }
if (-not $cfg.port) { $cfg.port = 8765 }
if (-not $cfg.Contains("max_download_mb")) { $cfg.max_download_mb = 4096 }
if (-not $cfg.Contains("token_auth")) { $cfg.token_auth = $true }
if (-not $cfg.Contains("enable_input")) { $cfg.enable_input = $false }
if (-not $cfg.Contains("allow_lan_urls")) { $cfg.allow_lan_urls = $false }
if (-not $cfg.Contains("dangerous_commands")) { $cfg.dangerous_commands = "ask" }
if (-not $cfg.Contains("enable_desktop")) { $cfg.enable_desktop = $true }
if (-not $cfg.Contains("console_log")) { $cfg.console_log = $true }
if (-not $cfg.Contains("console_diff_lines")) { $cfg.console_diff_lines = 80 }
if (-not $cfg.Contains("read_only")) { $cfg.read_only = $false }
if (-not $cfg.Contains("notifications")) { $cfg.notifications = $true }
if (-not $cfg.Contains("autostart")) { $cfg.autostart = $false }
if (-not $cfg.Contains("ai_check")) { $cfg.ai_check = "off" }
if (-not $cfg.Contains("ai_model")) { $cfg.ai_model = "3b" }
if (-not $cfg.Contains("ai_port")) { $cfg.ai_port = 8766 }
function Save-Config { ($cfg | ConvertTo-Json -Depth 5) | Set-Content -Path $CfgPath -Encoding UTF8 }
Save-Config

# ------------------------------------------------------------- python ------
Step "Проверяю Python 3.10+"
function Find-Python {
    foreach ($c in @("py", "python", "python3")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if (-not $cmd -or $cmd.Source -like "*WindowsApps*") { continue }
        try {
            $pa = if ($c -eq "py") { @("-3", "-c") } else { @("-c") }
            $v = & $c @pa "import sys;print(sys.executable);print(sys.version_info>=(3,10))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v.Count -ge 2 -and $v[1] -eq "True") { return $v[0] }
        } catch {}
    }
    $cands = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
    foreach ($p in $cands) {
        try { $ok = & $p.FullName -c "import sys;print(sys.version_info>=(3,10))" 2>$null; if ($ok -eq "True") { return $p.FullName } } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Warn "Python не найден."
    if (-not (AskYN "Установить Python 3.12 автоматически?" $true)) { Fail "Без Python мост не работает. Установите его с python.org и запустите START.bat снова." }
    $installed = $false
    if (Has-Winget) {
        try { winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements; $installed = ($LASTEXITCODE -eq 0) } catch {}
    }
    if (-not $installed) {
        $inst = Join-Path $ToolsDir "python-installer.exe"
        $pyFile = if ($Arch -eq "ARM64") { "python-3.12.10-arm64.exe" } else { "python-3.12.10-amd64.exe" }
        Download "https://www.python.org/ftp/python/3.12.10/$pyFile" $inst
        if (-not (Test-Signed $inst "Python Software Foundation")) {
            Remove-Item $inst -Force -ErrorAction SilentlyContinue
            Fail "Скачанный установщик Python не подписан Python Software Foundation - установка отменена. Установите Python 3.12 вручную с python.org"
        }
        Say "  Устанавливаю Python (1-2 минуты)..."
        Start-Process -FilePath $inst -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1" -Wait
    }
    Refresh-Path
    $py = Find-Python
    if (-not $py) { Fail "Python установился, но не найден. Перезапустите START.bat (или перезагрузите ПК)." }
}
Ok "Python: $py"

# ------------------------------------------------------------- packages ----
Step "Проверяю библиотеки Python"
if (-not (Test-Path $VenvPy)) {
    Say "  Создаю виртуальное окружение .venv ..."
    & $py -m venv (Join-Path $Root ".venv")
    if (-not (Test-Path $VenvPy)) { Fail "Не удалось создать .venv" }
}
# keep in sync with server\requirements.txt
$pkgs = @("mcp>=1.10,<2", "httpx>=0.27,<1", "pillow>=10.4,<13", "send2trash>=1.8,<2", "psutil>=5.9,<8", "pystray>=0.19,<1")
if ($cfg.enable_input) { $pkgs += "pyautogui>=0.9.54,<1" }
$marker = Join-Path $Root ".venv\pc-bridge-packages.txt"
$want = ($pkgs -join " ")
$have = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { "" }
if ($have -ne $want) {
    Say "  Устанавливаю: $want"
    & $VenvPy -m pip install --disable-pip-version-check -q --upgrade pip
    & $VenvPy -m pip install --disable-pip-version-check -q --upgrade @pkgs
    if ($LASTEXITCODE -ne 0) { Fail "pip не смог установить библиотеки (проверьте интернет)." }
    Set-Content -Path $marker -Value $want
}
& $VenvPy -c "import mcp.server.fastmcp, httpx, PIL, uvicorn, send2trash"
if ($LASTEXITCODE -ne 0) { Remove-Item $marker -ErrorAction SilentlyContinue; Fail "Библиотеки установлены с ошибкой. Запустите START.bat ещё раз." }
Ok "Библиотеки на месте"

# -------------------------------------------------------------- ngrok ------
Step "Проверяю ngrok"
function Find-Ngrok {
    $local = Join-Path $ToolsDir "ngrok.exe"
    if (Test-Path $local) { return $local }
    $c = Get-Command ngrok -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}
$NgrokMin = [version]"3.28.0"
function Get-NgrokVer($exe) {
    try { $t = (& $exe version 2>$null) -join " " } catch { return $null }
    if ($t -match "([0-9]+)\.([0-9]+)\.([0-9]+)") { return [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])" }
    return $null
}
function Install-Ngrok {
    $local = Join-Path $ToolsDir "ngrok.exe"
    $zip = Join-Path $ToolsDir "ngrok.zip"
    Get-Process ngrok -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $local } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    $ngFile = if ($Arch -eq "ARM64") { "ngrok-v3-stable-windows-arm64.zip" } else { "ngrok-v3-stable-windows-amd64.zip" }
    try { Download "https://bin.equinox.io/c/bNyj1mQVY4c/$ngFile" $zip }
    catch { Fail "Не удалось скачать ngrok: $($_.Exception.Message)" }
    if (Test-Path $local) { Remove-Item $local -Force -ErrorAction SilentlyContinue }
    Expand-Archive -Path $zip -DestinationPath $ToolsDir -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $local)) { Fail "ngrok.exe не появился. Возможно, его удалил антивирус - добавьте папку tools в исключения." }
    if (-not (Test-Signed $local "ngrok")) { Warn "У ngrok.exe нет действительной подписи ngrok - проверьте, что файл не подменён (SHA256: $((Get-FileHash $local).Hash))" }
    return $local
}
$ngrok = Find-Ngrok
if (-not $ngrok) {
    Say "  ngrok не найден - скачиваю..."
    $ngrok = Install-Ngrok
}
$ngVer = Get-NgrokVer $ngrok
if (-not $ngVer -or $ngVer -lt $NgrokMin) {
    Warn "ngrok $(if ($ngVer) { $ngVer } else { '(версия неизвестна)' }) устарел: бесплатный ngrok требует версию $NgrokMin или новее."
    Say "  Скачиваю свежую версию в папку tools..."
    $ngrok = Install-Ngrok
    $ngVer = Get-NgrokVer $ngrok
    if (-not $ngVer -or $ngVer -lt $NgrokMin) { Fail "Не удалось обновить ngrok (версия: $ngVer). Скачайте его вручную с https://ngrok.com/download и положите ngrok.exe в папку tools." }
}
Ok "ngrok $ngVer : $ngrok"

$ymls = @("$env:LOCALAPPDATA\ngrok\ngrok.yml", "$env:USERPROFILE\AppData\Local\ngrok\ngrok.yml", "$env:USERPROFILE\.ngrok2\ngrok.yml", "$env:USERPROFILE\.config\ngrok\ngrok.yml")
$hasToken = $false
foreach ($y in $ymls) { if ((Test-Path $y) -and (Select-String -Path $y -Pattern "authtoken" -Quiet)) { $hasToken = $true } }
if (-not $hasToken) {
    Warn "Нужен бесплатный токен ngrok (один раз)."
    Say "  Сейчас откроется сайт ngrok."
    Say "   1. Нет аккаунта? Нажмите Sign up и зарегистрируйтесь (бесплатно, можно через Google или GitHub)."
    Say "   2. После входа откройте раздел Your Authtoken (ссылка та же) и скопируйте токен."
    Say "   3. Вернитесь в это окно и вставьте токен (правый клик или Ctrl+V)."
    Start-Process "https://dashboard.ngrok.com/get-started/your-authtoken"
    $tok = (RH "  Вставьте authtoken и нажмите Enter").Trim()
    if (-not $tok) { Fail "Токен не введён." }
    & $ngrok config add-authtoken $tok
    if ($LASTEXITCODE -ne 0) { Fail "ngrok не принял токен." }
}
Ok "Токен ngrok настроен"

# ---- постоянный домен ngrok: спрашиваем ПОСЛЕ токена, когда аккаунт уже есть ----
if ($doWizard -or -not $cfg.Contains("ngrok_domain")) {
    Step "Постоянный адрес ngrok (необязательно, но удобно)"
    Say "  Без домена адрес для Notion будет новым при каждом запуске, и его придётся менять в настройках агента."
    Say "  С бесплатным доменом адрес постоянный - подключение в Notion настраивается один раз."
    Say "   1. Сейчас откроется страница Domains в ngrok (вы уже вошли в аккаунт)."
    Say "   2. Скопируйте ваш бесплатный домен, например abc-def.ngrok-free.app."
    Say "      Если списка доменов нет - создайте домен на этой странице (на бесплатном тарифе он один)."
    Say "   3. Вставьте его сюда. Не хотите домен - просто нажмите Enter."
    if (AskYN "Открыть страницу доменов ngrok?" $true) { Start-Process "https://dashboard.ngrok.com/domains" }
    $dom = AskText "Ваш домен ngrok ('-' = без домена)" $(if ($cfg.ngrok_domain) { $cfg.ngrok_domain } else { "-" })
    $dom = ($dom -replace '^https?://', '').Trim().TrimEnd('/')
    $cfg.ngrok_domain = if ($dom -eq "-" -or $dom -eq "") { "" } else { $dom }
    if ($cfg.ngrok_domain) { Ok "Домен: $($cfg.ngrok_domain)" } else { Warn "Без домена: адрес будет меняться при каждом запуске" }
    Save-Config
}

# ------------------------------------------------------------ blender ------
if ($cfg.enable_blender) {
    Step "Проверяю Blender"
    function Get-SteamLibraries {
        $roots = @()
        foreach ($k in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam', 'HKLM:\SOFTWARE\Valve\Steam')) {
            $p = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
            if ($p) { foreach ($n in @('SteamPath', 'InstallPath')) { if ($p.$n) { $roots += ($p.$n -replace '/', '\') } } }
        }
        $roots += @("${env:ProgramFiles(x86)}\Steam", "$env:ProgramFiles\Steam")
        foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
            foreach ($n in @('SteamLibrary', 'Steam', 'Games\Steam', 'Games\SteamLibrary')) { $roots += (Join-Path $d.Root $n) }
        }
        $libs = @()
        foreach ($r in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
            if (-not (Test-Path -LiteralPath $r)) { continue }
            $libs += $r
            foreach ($vdf in @("$r\steamapps\libraryfolders.vdf", "$r\config\libraryfolders.vdf")) {
                if (-not (Test-Path -LiteralPath $vdf)) { continue }
                $txt = [string](Get-Content -Raw -LiteralPath $vdf -ErrorAction SilentlyContinue)
                foreach ($m in [regex]::Matches($txt, '"(?:path|\d+)"\s+"([A-Za-z]:[^"]*)"')) {
                    $libs += ($m.Groups[1].Value -replace '\\\\', '\')
                }
            }
        }
        return $libs | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
    }
    function Find-Blender {
        $roots = @("$env:ProgramFiles\Blender Foundation", "${env:ProgramFiles(x86)}\Blender Foundation",
                   "$env:LOCALAPPDATA\Programs\Blender Foundation")
        # Steam: any library on any disk (D:\SteamLibrary ...), folder name from appmanifest_365670.acf
        foreach ($lib in (Get-SteamLibraries)) {
            $roots += "$lib\steamapps\common\Blender"
            $acf = "$lib\steamapps\appmanifest_365670.acf"
            if (Test-Path -LiteralPath $acf) {
                $m = [regex]::Match([string](Get-Content -Raw -LiteralPath $acf -ErrorAction SilentlyContinue), '"installdir"\s+"([^"]+)"')
                if ($m.Success) { $roots += "$lib\steamapps\common\$($m.Groups[1].Value)" }
            }
        }
        # Programs and Features (installer, Steam App 365670)
        $keys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
                  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*')
        foreach ($e in (Get-ItemProperty -Path $keys -ErrorAction SilentlyContinue)) {
            if (($e.DisplayName -like 'Blender*' -or $e.PSChildName -eq 'Steam App 365670') -and $e.InstallLocation) { $roots += $e.InstallLocation }
        }
        $found = @()
        foreach ($r in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
            if (-not (Test-Path -LiteralPath $r)) { continue }
            $direct = Join-Path $r 'blender.exe'
            if (Test-Path -LiteralPath $direct) { $found += Get-Item -LiteralPath $direct; continue }
            $found += Get-ChildItem -LiteralPath $r -Filter blender.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue
        }
        $b = $found | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($b) { return $b.FullName }
        $run = Get-Process blender -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1
        if ($run) { return $run.Path }
        $c = Get-Command blender -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
        return $null
    }
    if (-not ($cfg.blender_path -and (Test-Path $cfg.blender_path))) { $cfg.blender_path = Find-Blender }
    if (-not $cfg.blender_path) {
        Warn "Blender не найден."
        if ((Has-Winget) -and (AskYN "Установить Blender через winget?" $true)) {
            winget install -e --id BlenderFoundation.Blender --accept-package-agreements --accept-source-agreements
            $cfg.blender_path = Find-Blender
        }
        if (-not $cfg.blender_path) {
            $p = AskText "Путь к blender.exe (или '-' чтобы выключить модуль Blender)" "-"
            if ($p -ne "-" -and (Test-Path $p)) { $cfg.blender_path = $p } else { $cfg.enable_blender = $false; Warn "Модуль Blender выключен" }
        }
    }
    if ($cfg.blender_path) { Ok "Blender: $($cfg.blender_path)" }
}

# ------------------------------------------------------------- roblox ------
if ($cfg.enable_roblox) {
    Step "Проверяю Roblox Studio и Rojo"
    function Find-Studio {
        $dirs = @("$env:LOCALAPPDATA\Roblox\Versions", "${env:ProgramFiles(x86)}\Roblox\Versions", "$env:ProgramFiles\Roblox\Versions")
        $f = foreach ($d in $dirs) { if ($d -and (Test-Path $d)) { Get-ChildItem $d -Filter RobloxStudioBeta.exe -Recurse -ErrorAction SilentlyContinue } }
        $s = $f | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($s) { return $s.FullName } else { return $null }
    }
    $cfg.studio_path = Find-Studio
    if (-not $cfg.studio_path) {
        Warn "Roblox Studio не найден."
        if (AskYN "Скачать и запустить установщик Roblox Studio?" $true) {
            $inst = Join-Path $ToolsDir "RobloxStudioInstaller.exe"
            try {
                Download "https://setup.rbxcdn.com/RobloxStudioInstaller.exe" $inst
                if (-not (Test-Signed $inst "Roblox")) { Remove-Item $inst -Force; throw "bad signature" }
                Start-Process $inst
                RH "  Дождитесь окончания установки (войдите в аккаунт можно позже) и нажмите Enter"
            } catch { Warn "Не удалось скачать: установите Studio вручную с https://create.roblox.com" }
            $cfg.studio_path = Find-Studio
        }
    }
    if ($cfg.studio_path) { Ok "Roblox Studio: $($cfg.studio_path)" } else { Warn "Studio не найден - сборка .rbxlx всё равно будет работать" }

    function Get-RojoAsset($release, [string]$architecture) {
        $target = switch ($architecture) {
            "AMD64" { "x86_64" }
            "ARM64" { "aarch64" }
            default { throw "Rojo не поддерживает архитектуру Windows: $architecture" }
        }
        $asset = $release.assets | Where-Object { $_.name -match "-windows-$target\.zip$" } | Select-Object -First 1
        if (-not $asset) { throw "В выпуске Rojo $($release.tag_name) нет архива для Windows $target" }
        return $asset
    }
    function Test-Rojo([string]$exe) {
        if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $false }
        try {
            $version = & $exe --version 2>$null
            return ($LASTEXITCODE -eq 0 -and "$version" -match "\brojo\s+\d")
        } catch { return $false }
    }

    $rojoDir = Join-Path $ToolsDir "rojo"
    $rojo = Get-ChildItem -Path $rojoDir -Filter rojo.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($rojo -and -not (Test-Rojo $rojo.FullName)) {
        Warn "Найденный Rojo не запускается - скачаю версию для этого компьютера"
        $rojo = $null
    }
    if (-not $rojo) {
        Say "  Скачиваю Rojo (синхронизация файлов со Studio)..."
        try {
            $rel = Invoke-RestMethod "https://api.github.com/repos/rojo-rbx/rojo/releases/latest" -Headers @{ "User-Agent" = "pc-bridge" }
            $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
            $asset = Get-RojoAsset $rel $arch
            $zip = Join-Path $ToolsDir "rojo.zip"
            Download $asset.browser_download_url $zip
            Expand-Archive -Path $zip -DestinationPath $rojoDir -Force
            Remove-Item $zip -Force
            $rojo = Get-ChildItem -Path $rojoDir -Filter rojo.exe -Recurse | Select-Object -First 1
            $cfg.rojo_plugin_installed = $false
        } catch { Warn "Не удалось скачать Rojo: $($_.Exception.Message)" }
    }
    if ($rojo -and (Test-Rojo $rojo.FullName)) {
        $cfg.rojo_path = $rojo.FullName
        Ok "Rojo: $($cfg.rojo_path)"
        if ($cfg.studio_path -and -not $cfg.rojo_plugin_installed) {
            & $cfg.rojo_path plugin install
            if ($LASTEXITCODE -eq 0) { $cfg.rojo_plugin_installed = $true; Ok "Плагин Rojo установлен в Studio" }
        }
    } else {
        $cfg.rojo_path = $null
        $cfg.rojo_plugin_installed = $false
        Warn "Без рабочего Rojo будут работать только Studio и файлы"
    }
}
Save-Config

# ------------------------------------------------------------- unreal ------
if ($cfg.enable_unreal) {
    Step "Проверяю Unreal Engine 5"
    function Find-Unreal {
        $found = @()
        $dat = Join-Path $env:ProgramData "Epic\UnrealEngineLauncher\LauncherInstalled.dat"
        if (Test-Path $dat) {
            try {
                $j = Get-Content $dat -Raw -Encoding UTF8 | ConvertFrom-Json
                foreach ($it in $j.InstallationList) { if ($it.AppName -like "UE_5*") { $found += $it.InstallLocation } }
            } catch {}
        }
        foreach ($b in @($env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)})) {
            if ($b) {
                $eg = Join-Path $b "Epic Games"
                if (Test-Path $eg) { Get-ChildItem -Path $eg -Directory -Filter "UE_5*" -ErrorAction SilentlyContinue | ForEach-Object { $found += $_.FullName } }
            }
        }
        $ok = @()
        foreach ($r in $found) { if ($r -and (Test-Path (Join-Path $r "Engine\Binaries\Win64\UnrealEditor.exe"))) { $ok += $r } }
        return @($ok | Select-Object -Unique | Sort-Object -Descending)
    }
    $engines = @(Find-Unreal)
    if ($cfg.unreal_path -and (Test-Path (Join-Path $cfg.unreal_path "Engine\Binaries\Win64\UnrealEditor.exe"))) { $engines = @($cfg.unreal_path) + $engines }
    if ($engines.Count -eq 0) {
        Warn "Unreal Engine 5 не найден."
        Say "  Он ставится через Epic Games Launcher: Unreal Engine -> Library -> '+' -> Install (40-60 ГБ)." Yellow
        if ((Has-Winget) -and (AskYN "Установить Epic Games Launcher через winget?" $true)) {
            try { winget install -e --id EpicGames.EpicGamesLauncher --accept-package-agreements --accept-source-agreements | Out-Host } catch {}
            Say "  Откройте Epic Games Launcher, войдите, установите Unreal Engine 5 и запустите START.bat ещё раз." Yellow
        }
        $p = AskText "Папка движка, например C:\Program Files\Epic Games\UE_5.6 (или '-' чтобы выключить модуль)" "-"
        if ($p -and $p -ne "-" -and (Test-Path (Join-Path $p "Engine\Binaries\Win64\UnrealEditor.exe"))) {
            $cfg.unreal_path = $p
            $engines = @($p)
        } else {
            $cfg.enable_unreal = $false
            Warn "Модуль Unreal Engine выключен (включить: SETTINGS.bat)"
        }
    }
    if ($engines.Count -gt 0) {
        Ok ("Unreal Engine: " + ($engines -join ", "))
        if (-not $cfg.unreal_path) { $cfg.unreal_path = $engines[0] }
        $vs = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
        $hasVC = $false
        if (Test-Path $vs) { $hasVC = [bool](& $vs -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath) }
        if ($hasVC) { Ok "Visual Studio с C++ найден: C++-проекты будут собираться" }
        else { Say "  Для C++-проектов нужен Visual Studio 2022 с 'Game development with C++'. Blueprint-проекты работают без него." Yellow }
    }
}

# ------------------------------------------------------------- local AI ----
if ($cfg.ai_check -eq "on") {
    Step "ИИ-проверка команд (llama.cpp + Qwen2.5-Coder $($cfg.ai_model))"
    $LlamaDir = Join-Path $ToolsDir "llama"
    $llamaExe = Join-Path $LlamaDir "llama-server.exe"
    if (-not (Test-Path $llamaExe)) {
        $onPath = Get-Command llama-server -ErrorAction SilentlyContinue
        if ($onPath) { $llamaExe = $onPath.Source; Ok "Найден llama-server: $llamaExe" }
    }
    if (-not (Test-Path $llamaExe)) {
        try {
            # not /releases/latest: "latest" can be a release without Windows builds - take the newest one that has them
            $rels = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=40" -Headers @{ "User-Agent" = "pc-bridge" } -TimeoutSec 30
            $pats = if ($Arch -eq "ARM64") { @("win-cpu-arm64\.zip$", "win-arm64\.zip$") } else { @("win-cpu-x64\.zip$", "win-avx2-x64\.zip$", "win-x64\.zip$") }
            $asset = $null
            $tag = ""
            foreach ($r in @($rels)) {
                if ($r.draft) { continue }
                foreach ($pt in $pats) {
                    $asset = @($r.assets) | Where-Object { $_.name -match $pt -and $_.name -notmatch "cudart|cuda|hip|sycl|vulkan|opencl|kompute" } | Select-Object -First 1
                    if ($asset) { break }
                }
                if ($asset) { $tag = [string]$r.tag_name; break }
            }
            if (-not $asset) { throw "в последних релизах ggml-org/llama.cpp нет архива для Windows $Arch" }
            $zip = Join-Path $ToolsDir "llama.zip"
            Download $asset.browser_download_url $zip
            if (Test-Path $LlamaDir) { Remove-Item $LlamaDir -Recurse -Force }
            Expand-Archive -LiteralPath $zip -DestinationPath $LlamaDir -Force
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            $found = Get-ChildItem -Path $LlamaDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
            if ($found -and $found.DirectoryName -ne $LlamaDir) { Get-ChildItem -LiteralPath $found.DirectoryName | Move-Item -Destination $LlamaDir -Force }
            $cfg.llama_version = $tag
            if (Test-Path $llamaExe) { Ok "llama.cpp $tag установлен" }
        } catch { Warn "Не удалось скачать llama.cpp с GitHub: $($_.Exception.Message)" }
    }
    if (-not (Test-Path $llamaExe) -and (Has-Winget)) {
        Say "  Пробую установить llama.cpp через winget ..."
        try { & winget install --id ggml.llamacpp -e --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host } catch {}
        Refresh-Path
        $onPath = Get-Command llama-server -ErrorAction SilentlyContinue
        if ($onPath) { $llamaExe = $onPath.Source }
        else {
            $pk = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
            if (Test-Path $pk) {
                $f = Get-ChildItem -Path $pk -Directory -Filter "ggml.llamacpp*" -ErrorAction SilentlyContinue | ForEach-Object { Get-ChildItem -Path $_.FullName -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue } | Select-Object -First 1
                if ($f) { $llamaExe = $f.FullName }
            }
        }
        if (Test-Path $llamaExe) { Ok "llama.cpp установлен через winget: $llamaExe" }
    }
    if (-not (Test-Path $llamaExe)) {
        Warn "Вручную: скачайте llama-...-bin-win-cpu-x64.zip с https://github.com/ggml-org/llama.cpp/releases"
        Warn "и распакуйте в $LlamaDir (чтобы там лежал llama-server.exe), затем снова START.bat."
    }
    $models = @{
        "3b"   = @("https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF/resolve/main/qwen2.5-coder-3b-instruct-q4_k_m.gguf", 1200MB)
        "1.5b" = @("https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf", 700MB)
    }
    $m = $models[[string]$cfg.ai_model]
    if (-not $m) { $m = $models["3b"] }
    $ModelsDir = Join-Path $ToolsDir "models"
    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
    $modelFile = Join-Path $ModelsDir ([IO.Path]::GetFileName($m[0]))
    if (-not (Test-Path $modelFile) -or (Get-Item $modelFile).Length -lt $m[1]) {
        Say "  Скачиваю модель $([IO.Path]::GetFileName($m[0])) (один раз; при обрыве START.bat докачает) ..."
        $part = "$modelFile.part"
        & curl.exe -L --fail --retry 3 -C - -o "$part" "$($m[0])"
        if ((Test-Path $part) -and (Get-Item $part).Length -ge $m[1]) { Move-Item -LiteralPath $part -Destination $modelFile -Force }
    }
    if ((Test-Path $llamaExe) -and (Test-Path $modelFile) -and (Get-Item $modelFile).Length -ge $m[1]) {
        $cfg.llama_path = $llamaExe
        $cfg.ai_model_path = $modelFile
        Ok "ИИ-проверка готова - модель запустится вместе с мостом"
    } else {
        $cfg.llama_path = ""
        $cfg.ai_model_path = ""
        Warn "ИИ-проверка пока не работает (не скачалась) - команды проверяют только правила. Запустите START.bat ещё раз."
    }
    Save-Config
}

# ------------------------------------------------------------- start ------
Step "Запускаю сервер и туннель (в фоне, значок в трее)"
$port = [int]$cfg.port
if (Test-Port $port) { Fail "Порт $port уже занят другой программой. Запустите SETTINGS.bat и выберите другой порт." }
$srvErr = Join-Path $LogsDir "server.err.log"
$ngLog = Join-Path $LogsDir "ngrok.log"
$supLog = Join-Path $LogsDir "supervisor.log"
$statePath = Join-Path $LogsDir "state.json"

$others = Get-Process ngrok -ErrorAction SilentlyContinue
if ($others) {
    if (AskYN "Уже запущен другой ngrok (бесплатный тариф разрешает мало сессий). Закрыть его?" $true) { $others | Stop-Process -Force; Start-Sleep 1 }
}

# the supervisor starts ngrok itself: it needs the path and the right domain flag (--url since 3.16)
$cfg.ngrok_path = [string]$ngrok
$cfg.ngrok_url_flag = "url"
if ($cfg.ngrok_domain) {
    $verText = (& $ngrok version) -join " "
    if ($verText -match "(\d+)\.(\d+)\.") {
        $useUrl = ([int]$Matches[1] -gt 3) -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 16)
        if (-not $useUrl) { $cfg.ngrok_url_flag = "domain" }
    }
}
Save-Config

$VenvPyW = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $VenvPyW)) { $VenvPyW = $VenvPy }
$SupPy = Join-Path $Root "server\supervisor.py"
$StartupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "PC Bridge.lnk"
if ($cfg.autostart) {
    try {
        $sh = New-Object -ComObject WScript.Shell
        $lnk = $sh.CreateShortcut($StartupLnk)
        $lnk.TargetPath = $VenvPyW
        $lnk.Arguments = "`"$SupPy`""
        $lnk.WorkingDirectory = $Root
        $lnk.Description = "PC Bridge"
        $lnk.Save()
        Ok "Автозапуск при входе в Windows включён"
    } catch { Warn "Не удалось включить автозапуск: $($_.Exception.Message)" }
} elseif (Test-Path $StartupLnk) {
    Remove-Item -LiteralPath $StartupLnk -Force -ErrorAction SilentlyContinue
    Say "  Автозапуск выключен."
}

foreach ($f in @("stop.flag", "state.json")) { Remove-Item (Join-Path $LogsDir $f) -Force -ErrorAction SilentlyContinue }
$sup = Start-Process -FilePath $VenvPyW -ArgumentList @("`"$SupPy`"") -WorkingDirectory $Root -PassThru
$public = $null
$st = $null
$shown = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    if ($sup.HasExited) { break }
    if (Test-Path $statePath) {
        try { $st = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $st = $null }
        if ($st -and $st.server -eq "ok" -and -not $shown) { Ok "Сервер работает на порту $port"; $shown = $true }
        if ($st -and $st.url) { $public = [string]$st.url; break }
        if ($st -and ($st.ngrok -eq "error" -or $st.server -eq "port busy")) { break }
    }
}
if (-not $public) {
    if ($sup.HasExited) { Say "  Лог супервизора:"; Tail $supLog 20 }
    Say "  Лог сервера:"; Tail $srvErr 15
    Say "  Лог ngrok:"; Tail $ngLog 15
    $logText = if (Test-Path $ngLog) { Get-Content $ngLog -Raw } else { "" }
    if ($logText -match "ERR_NGROK_121") { Warn "ngrok слишком старый. Удалите tools\ngrok.exe и запустите START.bat снова - скачается новая версия." }
    if ($logText -match "ERR_NGROK_105|ERR_NGROK_107|ERR_NGROK_4018") { Warn "Неверный authtoken. Выполните: ngrok config add-authtoken ВАШ_ТОКЕН (https://dashboard.ngrok.com/get-started/your-authtoken)" }
    if ($logText -match "ERR_NGROK_108") { Warn "ngrok уже запущен на другом ПК или в другом окне. Закройте его (STOP.bat) и попробуйте снова." }
    if ($logText -match "ERR_NGROK_313|ERR_NGROK_319|ERR_NGROK_320") { Warn "Домен ngrok не ваш или указан с ошибкой. SETTINGS.bat -> укажите домен из https://dashboard.ngrok.com/domains" }
    if ($st -and $st.server -eq "port busy") { Warn "Порт $port заняла другая программа." }
    if ($st -and $st.server -ne "ok") { Warn "Сервер не запустился - см. лог сервера выше." }
    Stop-Old
    Fail "Мост не запустился (частые причины: неверный токен ngrok, чужой домен, уже запущен ngrok на другом ПК)."
}
$useToken = ($cfg.token_auth -ne $false)
$mcpUrl = if ($useToken) { "$($public.TrimEnd('/'))/mcp" } else { "$($public.TrimEnd('/'))/$($cfg.secret)/mcp" }
Set-Content -Path (Join-Path $Root "URL.txt") -Value $mcpUrl
$TokenPath = Join-Path $Root "TOKEN.txt"
if ($useToken) { Set-Content -Path $TokenPath -Value $cfg.secret -NoNewline } else { Remove-Item -LiteralPath $TokenPath -Force -ErrorAction SilentlyContinue }
try { Set-Clipboard -Value $mcpUrl } catch {}
Ok "Туннель: $public"

Step "Самопроверка через интернет"
try {
    $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"selftest","version":"VER"}}}'.Replace("VER", $Ver)
    $hdr = @{ "Accept" = "application/json, text/event-stream"; "ngrok-skip-browser-warning" = "1" }
    if ($useToken) { $hdr["Authorization"] = "Bearer $($cfg.secret)" }
    $r = Invoke-WebRequest -Uri $mcpUrl -Method Post -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 20 -Headers $hdr
    if ($r.StatusCode -eq 200 -and $r.Content -match "serverInfo") { Ok "Сервер отвечает из интернета" } else { Warn "Странный ответ: $($r.StatusCode)" }
} catch { Warn "Самопроверка не прошла: $($_.Exception.Message)" }
if ($useToken) {
    try {
        $null = Invoke-WebRequest -Uri $mcpUrl -Method Post -Body $body -ContentType "application/json" -UseBasicParsing -TimeoutSec 20 -Headers @{ "Accept" = "application/json, text/event-stream"; "ngrok-skip-browser-warning" = "1" }
        Warn "Сервер ответил без токена - так быть не должно. Проверьте token_auth в config.json"
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        if ($code -eq 401) { Ok "Без токена сервер никого не пускает" } else { Warn "Проверка без токена не удалась: $($_.Exception.Message)" }
    }
}

$mods = @("файлы", "код и git")
if ($cfg.enable_internet) { $mods += "интернет" }
if ($cfg.enable_blender)  { $mods += "Blender" }
if ($cfg.enable_roblox)   { $mods += "Roblox" }
if ($cfg.enable_unreal)   { $mods += "Unreal Engine" }
if ($cfg.enable_screen)   { $mods += "скриншоты" }
if ($cfg.enable_input)    { $mods += "мышь и клавиатура" }
if ($cfg.enable_shell)    { $mods += "командная строка" }
if ($cfg.enable_desktop)  { $mods += "буфер и процессы" }
if ($cfg.ai_check -eq "on" -and $cfg.llama_path) { $mods += "ИИ-проверка команд" }
if ($cfg.read_only)       { $mods += "ТОЛЬКО ЧТЕНИЕ" }

W ""
W "  ================================================================" -ForegroundColor Green
W "   ГОТОВО! Адрес для Notion (уже скопирован в буфер обмена):" -ForegroundColor Green
W ""
W "   $mcpUrl" -ForegroundColor White
if ($useToken) { W "   Вход по токену: токен в TOKEN.txt, клавиша T копирует его" -ForegroundColor White }
W ""
W "   Модули: $($mods -join ', ')" -ForegroundColor Green
W "  ================================================================" -ForegroundColor Green
Say ""
Say "  Как подключить к агенту (один раз, если адрес не меняется):" White
Say "   1. Откройте агента в Notion -> Настройки (Settings) -> Tools & Access -> Add connection"
Say "   2. Выберите Custom MCP server (свой MCP-сервер)"
Say "   3. В поле URL вставьте адрес (Ctrl+V) - он уже скопирован в буфер обмена"
if ($useToken) {
    Say "   4. Authentication: вход через заголовок (header / API key / bearer token)."
    Say "      Нажмите T в этом окне - токен скопируется - и вставьте его в Notion."
    Say "      Если Notion просит имя и значение заголовка:  Authorization  и  Bearer <токен>"
} else {
    Say "   4. В поле Authentication выберите None (без авторизации)."
    Say "      Логин, пароль или токен вводить НЕ нужно - секретный ключ уже спрятан в адресе"
}
Say "   5. Сохраните подключение (Connect)"
Say "   6. Напишите агенту: 'проверь PC Bridge, вызови server_info'"
Say ""
if ($useToken) { Say "  Адрес - в URL.txt, токен - в TOKEN.txt. Токен - это ключ к вашему ПК: никому его не показывайте." Yellow } else { Say "  Адрес также сохранён в URL.txt. Не показывайте его никому - это ключ к вашему ПК." Yellow }
if (-not $cfg.ngrok_domain) { Say "  Без своего домена адрес меняется при каждом запуске - придётся обновлять подключение." Yellow }
Say ""
Say "  Мост работает в фоне - значок PC Bridge в трее (возле часов). Это окно можно закрыть." Cyan
Say "  Меню значка: пауза, только чтение, уведомления, скопировать адрес/токен, остановить всё." Cyan
Say "  Горячие клавиши: Ctrl+Alt+Shift+P - пауза, Ctrl+Alt+Shift+S - остановить всё (или STOP.bat)." Cyan
Say "  Если ПК уснёт или пропадёт сеть - сервер и туннель перезапустятся сами." Cyan
Say ""
Say "  Ниже в этом окне - всё, что делает агент: вызовы, команды и изменения файлов (зелёный + / красный -)." Cyan
Say "  Эту же консоль открывает CONSOLE.bat или двойной щелчок по значку в трее." Cyan
Say "  U = адрес, T = токен, P = пауза, C = очистить, Q = остановить мост, Enter = закрыть окно (мост продолжит работать)." Cyan
Say ""
$consolePy = Join-Path $Root "server\console.py"
if ((Test-Path $VenvPy) -and (Test-Path $consolePy)) {
    & $VenvPy $consolePy --embedded
} else {
    RH "Нажмите Enter, чтобы закрыть"
}
