# PC Bridge: Windows OCR (Windows.Media.Ocr) for one image. Called by server.py:
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -Path image.png -Lang ru
# Prints UTF-8 JSON: {"lang":"ru","lines":[{"text":"...","words":[{"t":"...","x":0,"y":0,"w":0,"h":0}]}]}
# or {"error":"..."}.
param([string]$Path, [string]$Lang = "")
$ErrorActionPreference = "Stop"
$out = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
    function Await($op, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
        $null = $task.Wait(-1)
        $task.Result
    }
    $full = [IO.Path]::GetFullPath($Path)
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = $null
    if ($Lang) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
    }
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) {
        throw "No OCR language is installed. Windows Settings > Time & language > Language: add Russian/English (with text recognition)."
    }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = New-Object System.Collections.ArrayList
    foreach ($l in $result.Lines) {
        $words = New-Object System.Collections.ArrayList
        foreach ($w in $l.Words) {
            $r = $w.BoundingRect
            $null = $words.Add([ordered]@{ t = $w.Text; x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height })
        }
        $null = $lines.Add([ordered]@{ text = $l.Text; words = $words })
    }
    $stream.Dispose()
    $out = [ordered]@{ lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines }
} catch {
    $out = @{ error = $_.Exception.Message }
}
$json = ConvertTo-Json -InputObject $out -Depth 6 -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($json)
$so = [Console]::OpenStandardOutput()
$so.Write($bytes, 0, $bytes.Length)
$so.Flush()
