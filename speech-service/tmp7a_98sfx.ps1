
param([string]$Text, [string]$OutPath, [string]$VoiceName)
Add-Type -AssemblyName System.Speech
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($VoiceName) { $synth.SelectVoice($VoiceName) }
$synth.SetOutputToWaveFile($OutPath, $fmt)
$synth.Speak($Text)
$synth.Dispose()
Write-Output "OK"
