# Shared helpers: build Power BI semantic-model objects (TOM) from powerbi_build_spec.json.
# Dot-source this file:  . (Join-Path $PSScriptRoot 'PbiSpecModel.ps1')

function Get-PbiTabularDll {
    <# Path of Microsoft.PowerBI.Tabular.dll from the local Power BI Desktop install. #>
    param([string]$Path)
    if ($Path) { return $Path }
    $candidates = @()
    $pbi = Get-Process PBIDesktop -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pbi) { $candidates += Join-Path (Split-Path $pbi.Path) 'Microsoft.PowerBI.Tabular.dll' }
    $pkg = Get-AppxPackage -Name 'Microsoft.MicrosoftPowerBIDesktop' -ErrorAction SilentlyContinue
    if ($pkg) { $candidates += Join-Path $pkg.InstallLocation 'bin\Microsoft.PowerBI.Tabular.dll' }
    $candidates += Join-Path $env:ProgramFiles 'Microsoft Power BI Desktop\bin\Microsoft.PowerBI.Tabular.dll'
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) { throw 'Microsoft.PowerBI.Tabular.dll not found. Is Power BI Desktop installed?' }
    return $found
}

function Add-SpecObjects {
    <# Add the spec's Power Query parameters, tables, columns, partitions and measures to a TOM model. #>
    param($Model, $Spec, [hashtable]$Overrides = @{})
    $sm = $Spec.semanticModel
    $placeholders = @{ SourceServer = '<server>'; SourceWarehouse = '<warehouse>'; SourceDatabase = '<database>' }
    foreach ($p in $sm.parameters) {
        $value = $Overrides[$p.name]
        if (-not $value) { $value = if ($p.value -like '<set *') { $placeholders[$p.name] } else { $p.value } }
        $expr = New-Object Microsoft.AnalysisServices.Tabular.NamedExpression
        $expr.Name = $p.name
        $expr.Kind = [Microsoft.AnalysisServices.Tabular.ExpressionKind]::M
        $expr.Expression = '"' + $value.Replace('"', '""') + '" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
        $Model.Expressions.Add($expr)
        Write-Host "  + parameter $($p.name) = $value"
    }

    $types = @{ string = 'String'; int64 = 'Int64'; double = 'Double'; dateTime = 'DateTime'; boolean = 'Boolean' }
    foreach ($t in $sm.tables) {
        $calculated = $t.kind -eq 'calculated'
        $table = New-Object Microsoft.AnalysisServices.Tabular.Table
        $table.Name = $t.name
        $table.Description = if ($t.description) { $t.description }
                             elseif ($calculated) { "Shared by the MicroStrategy datasets that link attribute $($t.linkedAttribute)" }
                             else { "Migrated from MicroStrategy dataset $($t.mstrDatasetId) ($($t.mstrDatasetType))" }

        foreach ($c in $t.columns) {
            if ($calculated) {
                $col = New-Object Microsoft.AnalysisServices.Tabular.CalculatedTableColumn
                $col.IsNameInferred = $true
                $col.IsDataTypeInferred = $true
            } else {
                $col = New-Object Microsoft.AnalysisServices.Tabular.DataColumn
            }
            $col.Name = $c.name
            $col.SourceColumn = $c.sourceColumn
            $typeName = if ($c.dataType) { $types[$c.dataType] } else { 'String' }
            $col.DataType = [Microsoft.AnalysisServices.Tabular.DataType]::$typeName
            $col.IsHidden = [bool]$c.isHidden
            if ($c.dataCategory) { $col.DataCategory = $c.dataCategory }
            $col.SummarizeBy = if ($c.summarizeBy -eq 'sum') { [Microsoft.AnalysisServices.Tabular.AggregateFunction]::Sum }
                               else { [Microsoft.AnalysisServices.Tabular.AggregateFunction]::None }
            if ($c.mstr.attribute) { $col.Description = "MicroStrategy attribute $($c.mstr.attribute) / form $($c.mstr.form)" }
            elseif ($c.mstr.metricSource) { $col.Description = "Source column of MicroStrategy metric $($c.mstr.metricSource)" }
            $table.Columns.Add($col)
        }
        foreach ($c in $t.columns | Where-Object { $_.sortByColumn }) {
            $table.Columns[$c.name].SortByColumn = $table.Columns[$c.sortByColumn]
        }

        $part = New-Object Microsoft.AnalysisServices.Tabular.Partition
        $part.Mode = [Microsoft.AnalysisServices.Tabular.ModeType]::Import
        if ($calculated) {
            $part.Name = $t.name
            $src = New-Object Microsoft.AnalysisServices.Tabular.CalculatedPartitionSource
            $src.Expression = $t.expression
        } else {
            $part.Name = $t.partition.name
            $src = New-Object Microsoft.AnalysisServices.Tabular.MPartitionSource
            $src.Expression = $t.partition.expression
        }
        $part.Source = $src
        $table.Partitions.Add($part)

        foreach ($ms in $t.measures) {
            $measure = New-Object Microsoft.AnalysisServices.Tabular.Measure
            $measure.Name = $ms.name
            $measure.Expression = $ms.expression
            if ($ms.formatString) { $measure.FormatString = $ms.formatString }
            if ($ms.isHidden) { $measure.IsHidden = $true }
            $measure.Description = "MicroStrategy metric $($ms.mstr.metric). $($ms.note)"
            $table.Measures.Add($measure)
        }
        $Model.Tables.Add($table)
        Write-Host "  + table $($t.name): $($t.columns.Count) columns, $($t.measures.Count) measures"
    }

    foreach ($r in $sm.relationships) {
        $rel = New-Object Microsoft.AnalysisServices.Tabular.SingleColumnRelationship
        $rel.Name = [guid]::NewGuid().ToString()
        $rel.FromColumn = $Model.Tables[$r.fromTable].Columns[$r.fromColumn]
        $rel.FromCardinality = [Microsoft.AnalysisServices.Tabular.RelationshipEndCardinality]::Many
        $rel.ToColumn = $Model.Tables[$r.toTable].Columns[$r.toColumn]
        $rel.ToCardinality = [Microsoft.AnalysisServices.Tabular.RelationshipEndCardinality]::One
        $rel.CrossFilteringBehavior = [Microsoft.AnalysisServices.Tabular.CrossFilteringBehavior]::OneDirection
        $Model.Relationships.Add($rel)
        Write-Host "  + relationship $($r.fromTable)[$($r.fromColumn)] -> $($r.toTable)[$($r.toColumn)]"
    }
}
