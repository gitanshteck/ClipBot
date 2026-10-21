# Batch OCR with the built-in Windows OCR engine (no install needed).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -Dir <folder of jpg/png> -Out <jsonl file>
# Writes one JSON object per image: {"f":"name","lines":[{"t":"text","w":[{"t":"word","x":..,"y":..,"w":..,"h":..}]}]}
param(
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][string]$Out
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    [void]$task.Wait(-1)
    $task.Result
}
function J([string]$s) { '"' + ($s -replace '\\', '\\' -replace '"', '\"' -replace "[\x00-\x1f]", ' ') + '"' }

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { throw 'No OCR engine for the user profile languages' }

$files = Get-ChildItem -LiteralPath $Dir -File | Where-Object { $_.Extension -in '.jpg', '.png', '.jpeg' } | Sort-Object Name
$writer = New-Object System.IO.StreamWriter($Out, $false, (New-Object System.Text.UTF8Encoding($false)))
try {
    foreach ($f in $files) {
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.Append('{"f":' + (J $f.Name) + ',"lines":[')
        try {
            $sf = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($f.FullName)) ([Windows.Storage.StorageFile])
            $stream = Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
            $first = $true
            foreach ($l in $res.Lines) {
                if (-not $first) { [void]$sb.Append(',') }
                $first = $false
                [void]$sb.Append('{"t":' + (J $l.Text) + ',"w":[')
                $wf = $true
                foreach ($w in $l.Words) {
                    if (-not $wf) { [void]$sb.Append(',') }
                    $wf = $false
                    $r = $w.BoundingRect
                    [void]$sb.Append(('{{"t":{0},"x":{1},"y":{2},"w":{3},"h":{4}}}' -f (J $w.Text), [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height))
                }
                [void]$sb.Append(']}')
            }
            $stream.Dispose()
        }
        catch { [void]$sb.Append('],"err":' + (J $_.Exception.Message) + ',"x":['); }
        [void]$sb.Append(']}')
        $writer.WriteLine($sb.ToString())
    }
}
finally { $writer.Dispose() }
