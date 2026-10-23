<#
.SYNOPSIS
  Export the data model of the open Power BI Desktop file as TMDL (semantic model 'definition' folder).
.EXAMPLE
  .\scripts\export_model_tmdl.ps1 -OutFolder "3_powerbi\<ID8>\Sales Report.SemanticModel\definition"
#>
param(
    [Parameter(Mandatory = $true)][string]$OutFolder,
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'

$pbi = Get-Process PBIDesktop -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pbi) { throw "Power BI Desktop is not running." }
$engine = Get-CimInstance Win32_Process -Filter "Name = 'msmdsrv.exe'" |
    Where-Object { $_.ParentProcessId -eq $pbi.Id } | Select-Object -First 1
if (-not $engine) { $engine = Get-CimInstance Win32_Process -Filter "Name = 'msmdsrv.exe'" | Select-Object -First 1 }
if ($Port -eq 0) {
    $Port = (Get-NetTCPConnection -OwningProcess $engine.ProcessId -State Listen | Select-Object -First 1).LocalPort
}
Add-Type -Path (Join-Path (Split-Path $pbi.Path) 'Microsoft.PowerBI.Tabular.dll')

if (Test-Path $OutFolder) { throw "$OutFolder already exists; remove it first." }
$tom = New-Object Microsoft.AnalysisServices.Tabular.Server
$tom.Connect("Data Source=localhost:$Port")
try {
    $db = $tom.Databases[0]
    [Microsoft.AnalysisServices.Tabular.TmdlSerializer]::SerializeDatabaseToFolder($db, $OutFolder)
    Write-Host "Exported model of '$($pbi.MainWindowTitle)' ($($db.Model.Tables.Count) tables) to $OutFolder"
}
finally {
    $tom.Disconnect()
}
