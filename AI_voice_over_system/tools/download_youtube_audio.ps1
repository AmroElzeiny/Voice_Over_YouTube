param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [ValidateSet("chrome", "edge", "firefox", "brave")]
    [string]$Browser
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found in PATH. Install Python 3.11 or newer first."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg was not found in PATH. Install ffmpeg before running this helper."
}

python -c "import yt_dlp" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "yt-dlp is not installed. Run the project's normal requirements installation first."
}

$Arguments = @("$PSScriptRoot\local_youtube_audio.py", $Url)
if ($Browser) {
    $Arguments += @("--browser", $Browser)
}

& python @Arguments
exit $LASTEXITCODE
