<#
.SYNOPSIS
  Build the semantic model 'definition' folder (TMDL) of a Power BI project straight from powerbi_build_spec.json.

.DESCRIPTION
  Works offline: Power BI Desktop must be installed (its model library is used) but does not need to be running.
  Model settings mirror what Power BI Desktop 2.159 writes when saving a project.

.EXAMPLE
  .\scripts\build_model_tmdl.ps1 -SpecPath 2_work\<ID>\2_transform\powerbi_build_spec.json -OutFolder "3_powerbi\<ID8>\Sales Report.SemanticModel\definition"
#>
param(
    [Parameter(Mandatory = $true)][string]$SpecPath,
    [Parameter(Mandatory = $true)][string]$OutFolder,
    [string]$Server,
    [string]$Warehouse,
    [string]$Database,
    [string]$TabularDll
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'PbiSpecModel.ps1')
Add-Type -Path (Get-PbiTabularDll $TabularDll)

if (Test-Path $OutFolder) { throw "$OutFolder already exists; remove it first." }
$spec = Get-Content $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json

$db = New-Object Microsoft.AnalysisServices.Tabular.Database
$db.Name = $spec.report.name
$db.CompatibilityLevel = 1606
$db.CompatibilityMode = [Microsoft.AnalysisServices.CompatibilityMode]::PowerBI
$db.Language = 1033

$m = New-Object Microsoft.AnalysisServices.Tabular.Model
$db.Model = $m
$m.Culture = 'en-US'
$m.SourceQueryCulture = 'en-US'
$m.DefaultPowerBIDataSourceVersion = [Microsoft.AnalysisServices.Tabular.PowerBIDataSourceVersion]::PowerBI_V3
$m.DataAccessOptions.LegacyRedirects = $true
$m.DataAccessOptions.ReturnErrorValuesAsNull = $true
$culture = New-Object Microsoft.AnalysisServices.Tabular.Culture
$culture.Name = 'en-US'
$m.Cultures.Add($culture)

Add-SpecObjects -Model $m -Spec $spec -Overrides @{ SourceServer = $Server; SourceWarehouse = $Warehouse; SourceDatabase = $Database }

$queryOrder = @($spec.semanticModel.tables | Where-Object { $_.kind -ne 'calculated' } | ForEach-Object { $_.name }) +
              @($spec.semanticModel.parameters | ForEach-Object { $_.name })
foreach ($a in @(@('__PBI_TimeIntelligenceEnabled', '1'), @('PBI_QueryOrder', (ConvertTo-Json -InputObject $queryOrder -Compress)))) {
    $ann = New-Object Microsoft.AnalysisServices.Tabular.Annotation
    $ann.Name = $a[0]
    $ann.Value = $a[1]
    $m.Annotations.Add($ann)
}

[Microsoft.AnalysisServices.Tabular.TmdlSerializer]::SerializeDatabaseToFolder($db, $OutFolder)
Write-Host "Model written to $OutFolder"
