param(
    [ValidateSet("p0", "p1", "p2", "post", "manual", "customer", "all")]
    [string]$Suite = "all",

    [ValidateSet("schema", "strict", "compat", "business")]
    [string]$Profile = "schema",

    [Alias("Input")]
    [string]$CasesInput = "data\new_api_compare_cases.csv",
    [string]$OldBase = "",
    [string]$NewBase = "",
    [string]$PostBodyCases = "config\post_body_cases.csv",
    [string]$PostCasesJson = "config\post_cases.json",

    [int]$Workers = 4,
    [int]$PostBodyLimitPerRoute = 10,
    [int]$Limit = 0,
    [int]$Retries = 2,
    [double]$RetrySleep = 1.0,
    [double]$RequestDelay = 0.15,
    [double]$ProgressInterval = 5.0,
    [ValidateSet("off", "static", "id")]
    [string]$ManualDerived = "off",
    [int]$BoundaryPage = 5,
    [int]$BoundaryPerPage = 100,
    [switch]$PaginationBoundary,
    [switch]$DryRun,
    [switch]$NoPaginationBoundary,
    [switch]$OpenReport,
    [switch]$StrictExit
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

try {
    chcp 65001 | Out-Null
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    Write-Host "WARN: failed to switch console to UTF-8. Continue."
}

function Require-File {
    param([string]$RelativePath)

    $Path = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing required file: $RelativePath"
    }
    return $Path
}

function Value-Or-Zero {
    param($Value)

    if ($null -eq $Value) {
        return 0
    }
    return $Value
}

function Write-RunLog {
    param(
        [string]$Message,
        [string]$Path,
        [switch]$Echo
    )

    if ($Echo) {
        Write-Host $Message
    }
    if ($Path) {
        $Utf8 = [System.Text.UTF8Encoding]::new($false)
        $Bytes = $Utf8.GetBytes([string]$Message + [Environment]::NewLine)
        $LogStream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::ReadWrite
        )
        try {
            $null = $LogStream.Seek(0, [System.IO.SeekOrigin]::End)
            $LogStream.Write($Bytes, 0, $Bytes.Length)
        } finally {
            $LogStream.Dispose()
        }
    }
}

function Show-NewLogLines {
    param(
        [string]$Path,
        [ref]$ByteOffset
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $Stream = $null
    $Reader = $null
    try {
        $Stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        if ($ByteOffset.Value -gt $Stream.Length) {
            $ByteOffset.Value = 0
        }
        $null = $Stream.Seek($ByteOffset.Value, [System.IO.SeekOrigin]::Begin)
        $Reader = New-Object System.IO.StreamReader(
            $Stream,
            [System.Text.UTF8Encoding]::new($false),
            $false,
            4096,
            $true
        )
        while ($null -ne ($Line = $Reader.ReadLine())) {
            Write-Host $Line
        }
        $ByteOffset.Value = $Stream.Position
    } finally {
        if ($Reader) {
            $Reader.Dispose()
        } elseif ($Stream) {
            $Stream.Dispose()
        }
    }
}

function Join-ProcessArguments {
    param([object[]]$Arguments)

    $Quoted = foreach ($Arg in $Arguments) {
        $Text = [string]$Arg
        '"' + ($Text -replace '"', '\"') + '"'
    }
    return ($Quoted -join " ")
}

function Load-Config {
    $ConfigPath = Join-Path $Root "config\config.json"
    Require-File "config\config.json" | Out-Null
    return Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Html-Escape {
    param($Value)

    return [System.Net.WebUtility]::HtmlEncode([string]$Value)
}

function Get-PythonCommand {
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        return $VenvPython
    }
    return "python"
}

function Get-SuiteConfig {
    param([string]$Name)

    $Suites = @{
        all = @{
            Label = "All API cases"
            Input = $CasesInput
            Extra = @()
        }
        p0 = @{
            Label = "P0 smoke suite"
            Input = $CasesInput
            Extra = @("--case-priority", "p0")
        }
        p1 = @{
            Label = "P1 main compatibility suite"
            Input = $CasesInput
            Extra = @("--case-priority", "p1")
        }
        p2 = @{
            Label = "P2 high-frequency regression suite"
            Input = $CasesInput
            Extra = @("--case-priority", "p2")
        }
        post = @{
            Label = "POST API suite"
            Input = $CasesInput
            Extra = @("--method", "POST")
        }
        manual = @{
            Label = "Manual regression suite"
            Input = $CasesInput
            Extra = @("--case-priority", "manual")
        }
        customer = @{
            Label = "Customer request suite"
            Input = $CasesInput
            Extra = @("--case-priority", "customer")
        }
    }

    return $Suites[$Name]
}

function Invoke-CompareSuite {
    param([string]$SuiteName)

    Require-File "compare_apis.py" | Out-Null

    $Config = Load-Config
    $Python = Get-PythonCommand
    $SuiteConfig = Get-SuiteConfig $SuiteName
    $SuiteLabel = [string]$SuiteConfig["Label"]
    $InputFile = [string]$SuiteConfig["Input"]
    $SuiteExtra = @($SuiteConfig["Extra"])

    Require-File $InputFile | Out-Null

    $Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $RunName = "$SuiteName`_$Timestamp"
    $ReportDir = Join-Path $Root "reports\$RunName"
    $LogDir = Join-Path $Root "logs"
    $LogPath = Join-Path $LogDir "$RunName.log"

    New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $LogByteOffset = 0

    $CompareArgs = @(
        "compare_apis.py",
        "--input", $InputFile,
        "--out", $ReportDir,
        "--console-log", $LogPath,
        "--quiet-console",
        "--workers", [string]$Workers,
        "--profile", $Profile,
        "--post-body-cases", $PostBodyCases,
        "--post-cases-json", $PostCasesJson,
        "--post-body-limit-per-route", [string]$PostBodyLimitPerRoute,
        "--retries", [string]$Retries,
        "--retry-sleep", [string]$RetrySleep,
        "--request-delay", [string]$RequestDelay,
        "--progress-interval", [string]$ProgressInterval,
        "--manual-derived-mode", $ManualDerived
    ) + $SuiteExtra

    if ($Limit -gt 0) {
        $CompareArgs += @("--limit", [string]$Limit)
    }
    if ($OldBase) {
        $CompareArgs += @("--old-base", $OldBase)
    }
    if ($NewBase) {
        $CompareArgs += @("--new-base", $NewBase)
    }
    if ($DryRun) {
        $CompareArgs += @("--dry-run")
    }

    # Pagination boundary is not an app-reachable path, so it is opt-in.
    if ($PaginationBoundary -and -not $NoPaginationBoundary) {
        $CompareArgs += @("--pagination-boundary", "--boundary-page", [string]$BoundaryPage, "--boundary-per-page", [string]$BoundaryPerPage)
    }

    $CompareExitCode = 1
    $CompareExitCodeKnown = $true
    $FinalExitCode = 1

    try {
        Set-Content -LiteralPath $LogPath -Value @(
            "API Compare"
            "Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        ) -Encoding UTF8

        Write-RunLog "============================================================" $LogPath
        Write-RunLog "API Compare" $LogPath
        Write-RunLog "============================================================" $LogPath
        Write-RunLog "Run ID:      $RunName" $LogPath
        Write-RunLog "Suite:       $SuiteLabel" $LogPath
        $EffectiveOldBase = if ($OldBase) { $OldBase } else { $Config.old_base }
        $EffectiveNewBase = if ($NewBase) { $NewBase } else { $Config.new_base }
        Write-RunLog "Old API:     $EffectiveOldBase" $LogPath
        Write-RunLog "New API:     $EffectiveNewBase" $LogPath
        Write-RunLog "Input:       $InputFile" $LogPath
        Write-RunLog "Report dir:  $ReportDir" $LogPath
        Write-RunLog "Log file:    $LogPath" $LogPath
        Write-RunLog "Profile:     $Profile" $LogPath
        if ($Profile -eq "schema") {
            Write-RunLog "Check mode:  schema/field/type only; ignore values, list IDs, and list lengths" $LogPath
        } else {
            Write-RunLog "Check mode:  $Profile compatibility rules" $LogPath
        }
        Write-RunLog "Workers:     $Workers" $LogPath
        if ($Limit -gt 0) {
            Write-RunLog "Limit:       first $Limit request rows" $LogPath
        }
        if ($DryRun) {
            Write-RunLog "Dry run:     enabled, no real HTTP requests" $LogPath
        }
        Write-RunLog "Retries:     $Retries, sleep=$RetrySleep sec" $LogPath
        Write-RunLog "Request delay: $RequestDelay sec" $LogPath
        Write-RunLog "Progress:    every $ProgressInterval sec" $LogPath
        Write-RunLog "Manual derived: $ManualDerived" $LogPath
        if ($PaginationBoundary -and -not $NoPaginationBoundary) {
            Write-RunLog "Boundary:    enabled, p=$BoundaryPage, per_page=$BoundaryPerPage" $LogPath
        } else {
            Write-RunLog "Boundary:    disabled" $LogPath
        }
        Write-RunLog "Command:     $Python $($CompareArgs -join ' ')" $LogPath
        Write-RunLog "============================================================" $LogPath
        Show-NewLogLines $LogPath ([ref]$LogByteOffset)

        $PreviousPythonUnbuffered = $env:PYTHONUNBUFFERED
        $PreviousPythonIoEncoding = $env:PYTHONIOENCODING
        $env:PYTHONUNBUFFERED = "1"
        $env:PYTHONIOENCODING = "utf-8"

        $ProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $ProcessInfo.FileName = $Python
        $ProcessInfo.Arguments = Join-ProcessArguments $CompareArgs
        $ProcessInfo.UseShellExecute = $false
        $ProcessInfo.CreateNoWindow = $true
        $ProcessInfo.RedirectStandardOutput = $true
        $ProcessInfo.RedirectStandardError = $true
        $ProcessInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $ProcessInfo.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)

        $Process = [System.Diagnostics.Process]::new()
        $Process.StartInfo = $ProcessInfo
        $null = $Process.Start()

        while (-not $Process.WaitForExit(500)) {
            Show-NewLogLines $LogPath ([ref]$LogByteOffset)
        }
        Show-NewLogLines $LogPath ([ref]$LogByteOffset)
        $ProcessStdout = $Process.StandardOutput.ReadToEnd()
        $ProcessStderr = $Process.StandardError.ReadToEnd()
        if ($null -ne $Process.ExitCode) {
            $CompareExitCode = [int]$Process.ExitCode
        } else {
            $CompareExitCode = 0
            $CompareExitCodeKnown = $false
        }

        $ProcessErrors = @($ProcessStderr -split "`r?`n" | Where-Object { $_ -ne "" })
        if ($ProcessErrors.Count -gt 0) {
            Write-RunLog "Process stderr:" $LogPath
            foreach ($Line in $ProcessErrors) {
                Write-RunLog $Line $LogPath
            }
            Show-NewLogLines $LogPath ([ref]$LogByteOffset)
        }
        New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
        if ($ProcessStdout.Trim()) {
            Set-Content -LiteralPath (Join-Path $ReportDir "process_stdout.txt") -Value $ProcessStdout -Encoding UTF8
        }
        if ($ProcessStderr.Trim()) {
            Set-Content -LiteralPath (Join-Path $ReportDir "process_stderr.txt") -Value $ProcessStderr -Encoding UTF8
        }

        if ($null -eq $PreviousPythonUnbuffered) {
            Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONUNBUFFERED = $PreviousPythonUnbuffered
        }
        if ($null -eq $PreviousPythonIoEncoding) {
            Remove-Item Env:\PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $PreviousPythonIoEncoding
        }

        $SummaryPath = Join-Path $ReportDir "summary.json"
        $ReportPath = Join-Path $ReportDir "report.html"
        $IssueSummaryPath = Join-Path $ReportDir "issue_summary.csv"
        $FailedRequestsPath = Join-Path $ReportDir "failed_requests.csv"

        if (Test-Path -LiteralPath $SummaryPath) {
            $Summary = Get-Content -LiteralPath $SummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $PassCount = Value-Or-Zero $Summary.pass
            $FailCount = Value-Or-Zero $Summary.fail
            $ErrorCount = Value-Or-Zero $Summary.error

            if (-not $CompareExitCodeKnown) {
                if ($FailCount -gt 0 -or $ErrorCount -gt 0) {
                    $CompareExitCode = 1
                } else {
                    $CompareExitCode = 0
                }
            }
            Write-RunLog "============================================================" $LogPath
            Write-RunLog "Compare process exit code: $CompareExitCode" $LogPath
            Write-RunLog "Completed." $LogPath
            Write-RunLog "Cases: total=$($Summary.executed_cases), pass=$PassCount, fail=$FailCount, error=$ErrorCount, skipped=$($Summary.skipped_cases)" $LogPath
            Write-RunLog "Avg elapsed: old=$($Summary.avg_old_elapsed_ms)ms, new=$($Summary.avg_new_elapsed_ms)ms" $LogPath

            if ($Summary.by_category) {
                Write-RunLog "" $LogPath
                Write-RunLog "Failure categories:" $LogPath
                $Summary.by_category.PSObject.Properties |
                    Sort-Object Name |
                    ForEach-Object {
                        Write-RunLog ("  {0}: {1}" -f $_.Name, $_.Value) $LogPath
                    }
            }

            if ($Summary.by_case_priority) {
                Write-RunLog "" $LogPath
                Write-RunLog "Case priority (suite) summary:" $LogPath
                $Summary.by_case_priority.PSObject.Properties |
                    Sort-Object Name |
                    ForEach-Object {
                        $PriorityFail = 0
                        if ($Summary.fail_by_case_priority -and $Summary.fail_by_case_priority.PSObject.Properties[$_.Name]) {
                            $PriorityFail = $Summary.fail_by_case_priority.PSObject.Properties[$_.Name].Value
                        }
                        Write-RunLog ("  {0}: executed={1}, fail_or_error={2}" -f $_.Name, $_.Value, $PriorityFail) $LogPath
                    }
            }

            if ($Summary.by_platform) {
                Write-RunLog "" $LogPath
                Write-RunLog "Platform summary:" $LogPath
                $Summary.by_platform.PSObject.Properties |
                    Sort-Object Name |
                    ForEach-Object {
                        $PlatformFailCount = 0
                        if ($Summary.fail_by_platform -and $Summary.fail_by_platform.PSObject.Properties[$_.Name]) {
                            $PlatformFailCount = $Summary.fail_by_platform.PSObject.Properties[$_.Name].Value
                        }
                        Write-RunLog ("  {0}: total={1}, fail_or_error={2}" -f $_.Name, $_.Value, $PlatformFailCount) $LogPath
                    }
            }

            if ($Summary.top_failed_routes) {
                Write-RunLog "" $LogPath
                Write-RunLog "Top failed routes:" $LogPath
                $Summary.top_failed_routes |
                    Select-Object -First 10 |
                    ForEach-Object {
                        Write-RunLog ("  {0}: {1}" -f $_[0], $_[1]) $LogPath
                    }
            }

            Write-RunLog "" $LogPath
            Write-RunLog "Output files:" $LogPath
            Write-RunLog "  HTML report:     $ReportPath" $LogPath
            Write-RunLog "  Summary JSON:    $SummaryPath" $LogPath
            Write-RunLog "  Issue summary:   $IssueSummaryPath" $LogPath
            Write-RunLog "  Failed requests: $FailedRequestsPath" $LogPath
            Write-RunLog "  Full log:        $LogPath" $LogPath

            if ($FailCount -gt 0 -or $ErrorCount -gt 0) {
                Write-RunLog "" $LogPath
                Write-RunLog "Result: completed with API differences or errors. Open report.html and issue_summary.csv first." $LogPath
            } else {
                Write-RunLog "" $LogPath
                Write-RunLog "Result: completed with no differences." $LogPath
            }

            if ($OpenReport -and (Test-Path -LiteralPath $ReportPath)) {
                Start-Process -FilePath $ReportPath
            }

            if ($StrictExit) {
                $FinalExitCode = $CompareExitCode
            } else {
                $FinalExitCode = 0
            }
            Show-NewLogLines $LogPath ([ref]$LogByteOffset)
        } else {
            if (-not $CompareExitCodeKnown) {
                $CompareExitCode = 1
            }
            Write-RunLog "============================================================" $LogPath
            Write-RunLog "Compare process exit code: $CompareExitCode" $LogPath
            Write-RunLog "summary.json was not generated. Check log: $LogPath" $LogPath
            $FinalExitCode = $CompareExitCode
            Show-NewLogLines $LogPath ([ref]$LogByteOffset)
        }
    } finally {
        try {
            $FinishedLine = "Finished: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
            $Utf8 = [System.Text.UTF8Encoding]::new($false)
            $FinishedBytes = $Utf8.GetBytes($FinishedLine + [Environment]::NewLine)
            $FinishedStream = [System.IO.File]::Open(
                $LogPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            try {
                $FinishedStream.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
                $FinishedStream.Write($FinishedBytes, 0, $FinishedBytes.Length)
            } finally {
                $FinishedStream.Dispose()
            }
        } catch {
            Write-Host "WARN: could not append finished marker to log."
        }
        Show-NewLogLines $LogPath ([ref]$LogByteOffset)
    }

    return [pscustomobject]@{
        Suite = $SuiteName
        RunName = $RunName
        ExitCode = $FinalExitCode
        CompareExitCode = $CompareExitCode
        ReportDir = $ReportDir
        ReportPath = Join-Path $ReportDir "report.html"
        SummaryPath = Join-Path $ReportDir "summary.json"
        IssueSummaryPath = Join-Path $ReportDir "issue_summary.csv"
        FailedRequestsPath = Join-Path $ReportDir "failed_requests.csv"
        LogPath = $LogPath
    }
}

function Write-AllSummaryReport {
    param([object[]]$Runs)

    $Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutDir = Join-Path $Root "reports\all_$Timestamp"
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

    $TotalCases = 0
    $TotalPass = 0
    $TotalFail = 0
    $TotalError = 0
    $TotalSkipped = 0
    $TotalIssues = 0
    $CategoryCounts = @{}
    $RouteCounts = @{}
    $PlatformCounts = @{}
    $PlatformFailCounts = @{}
    $Rows = @()

    foreach ($Run in $Runs) {
        if (-not (Test-Path -LiteralPath $Run.SummaryPath)) {
            $Rows += "<tr><td>$(Html-Escape $Run.Suite)</td><td colspan='8'>summary.json 未生成：$(Html-Escape $Run.ReportDir)</td></tr>"
            continue
        }

        $Summary = Get-Content -LiteralPath $Run.SummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $Cases = [int](Value-Or-Zero $Summary.executed_cases)
        $Pass = [int](Value-Or-Zero $Summary.pass)
        $Fail = [int](Value-Or-Zero $Summary.fail)
        $ErrorCount = [int](Value-Or-Zero $Summary.error)
        $Skipped = [int](Value-Or-Zero $Summary.skipped_cases)
        $Issues = [int](Value-Or-Zero $Summary.issue_group_count)

        $TotalCases += $Cases
        $TotalPass += $Pass
        $TotalFail += $Fail
        $TotalError += $ErrorCount
        $TotalSkipped += $Skipped
        $TotalIssues += $Issues

        if ($Summary.by_category) {
            foreach ($Prop in $Summary.by_category.PSObject.Properties) {
                if (-not $CategoryCounts.ContainsKey($Prop.Name)) {
                    $CategoryCounts[$Prop.Name] = 0
                }
                $CategoryCounts[$Prop.Name] += [int]$Prop.Value
            }
        }
        if ($Summary.top_failed_routes) {
            foreach ($Item in $Summary.top_failed_routes) {
                $Route = [string]$Item[0]
                if (-not $RouteCounts.ContainsKey($Route)) {
                    $RouteCounts[$Route] = 0
                }
                $RouteCounts[$Route] += [int]$Item[1]
            }
        }
        if ($Summary.by_platform) {
            foreach ($Prop in $Summary.by_platform.PSObject.Properties) {
                if (-not $PlatformCounts.ContainsKey($Prop.Name)) {
                    $PlatformCounts[$Prop.Name] = 0
                }
                $PlatformCounts[$Prop.Name] += [int]$Prop.Value
            }
        }
        if ($Summary.fail_by_platform) {
            foreach ($Prop in $Summary.fail_by_platform.PSObject.Properties) {
                if (-not $PlatformFailCounts.ContainsKey($Prop.Name)) {
                    $PlatformFailCounts[$Prop.Name] = 0
                }
                $PlatformFailCounts[$Prop.Name] += [int]$Prop.Value
            }
        }

        $RelReport = "../$($Run.RunName)/report.html"
        $RelIssue = "../$($Run.RunName)/issue_summary.csv"
        $RelFailed = "../$($Run.RunName)/failed_requests.csv"
        $Rows += @"
<tr>
  <td><b>$(Html-Escape $Run.Suite)</b><br><span>$(Html-Escape $Run.RunName)</span></td>
  <td>$Cases</td><td class="ok">$Pass</td><td class="bad">$Fail</td><td class="bad">$ErrorCount</td><td>$Skipped</td><td>$Issues</td>
  <td><a href="$(Html-Escape $RelReport)">report.html</a></td>
  <td><a href="$(Html-Escape $RelIssue)">issue_summary.csv</a> · <a href="$(Html-Escape $RelFailed)">failed_requests.csv</a></td>
</tr>
"@
    }

    $CategoryRows = ($CategoryCounts.GetEnumerator() | Sort-Object Name | ForEach-Object {
        "<tr><td>$(Html-Escape $_.Name)</td><td>$($_.Value)</td></tr>"
    }) -join "`n"
    if (-not $CategoryRows) {
        $CategoryRows = "<tr><td colspan='2'>无失败分类</td></tr>"
    }

    $RouteRows = ($RouteCounts.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 20 | ForEach-Object {
        "<tr><td>$(Html-Escape $_.Name)</td><td>$($_.Value)</td></tr>"
    }) -join "`n"
    if (-not $RouteRows) {
        $RouteRows = "<tr><td colspan='2'>无失败路由</td></tr>"
    }

    $PlatformRows = ($PlatformCounts.GetEnumerator() | Sort-Object Name | ForEach-Object {
        $FailCount = 0
        if ($PlatformFailCounts.ContainsKey($_.Name)) {
            $FailCount = $PlatformFailCounts[$_.Name]
        }
        "<tr><td>$(Html-Escape $_.Name)</td><td>$($_.Value)</td><td>$FailCount</td></tr>"
    }) -join "`n"
    if (-not $PlatformRows) {
        $PlatformRows = "<tr><td colspan='3'>无平台数据</td></tr>"
    }

    $SummaryJson = [pscustomobject]@{
        generated_at = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        suites = @($Runs | ForEach-Object { $_.RunName })
        executed_cases = $TotalCases
        pass = $TotalPass
        fail = $TotalFail
        error = $TotalError
        skipped_cases = $TotalSkipped
        issue_group_count = $TotalIssues
        by_category = $CategoryCounts
        by_platform = $PlatformCounts
        fail_by_platform = $PlatformFailCounts
        top_failed_routes = $RouteCounts
    }
    $SummaryJson | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutDir "summary.json") -Encoding UTF8

    $Html = @"
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>API Compare All Summary</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,Arial,sans-serif;margin:0;background:#f3f6fb;color:#111827}
main{padding:24px 32px}
h1{margin:0 0 8px}
.muted,span{color:#64748b;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px}
.card b{display:block;color:#64748b;font-size:13px}.card strong{font-size:26px}
table{width:100%;border-collapse:collapse;background:#fff;margin:14px 0 24px}
th,td{border-bottom:1px solid #e2e8f0;padding:10px;text-align:left;vertical-align:top;font-size:13px}
th{background:#f8fafc}.ok{color:#16a34a;font-weight:700}.bad{color:#dc2626;font-weight:700}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
</style>
</head>
<body><main>
<h1>API Compare 全部套件汇总</h1>
<div class="muted">生成时间：$(Html-Escape (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))；详细问题仍以各套件 report.html 为准。</div>
<div class="cards">
  <div class="card"><b>执行用例</b><strong>$TotalCases</strong></div>
  <div class="card"><b>通过</b><strong class="ok">$TotalPass</strong></div>
  <div class="card"><b>失败</b><strong class="bad">$TotalFail</strong></div>
  <div class="card"><b>错误</b><strong class="bad">$TotalError</strong></div>
  <div class="card"><b>聚合问题</b><strong>$TotalIssues</strong></div>
</div>
<h2>套件结果</h2>
<table><tr><th>套件</th><th>执行</th><th>通过</th><th>失败</th><th>错误</th><th>跳过</th><th>聚合问题</th><th>HTML 报告</th><th>CSV 明细</th></tr>
$($Rows -join "`n")
</table>
<h2>失败分类汇总</h2>
<table><tr><th>分类</th><th>数量</th></tr>$CategoryRows</table>
<h2>平台汇总</h2>
<table><tr><th>平台</th><th>执行数</th><th>失败/错误数</th></tr>$PlatformRows</table>
<h2>失败路由 Top 20 汇总</h2>
<table><tr><th>路由</th><th>失败数</th></tr>$RouteRows</table>
</main></body></html>
"@
    $IndexPath = Join-Path $OutDir "index.html"
    Set-Content -LiteralPath $IndexPath -Value $Html -Encoding UTF8

    Write-Host ""
    Write-Host "============================================================"
    Write-Host "All suites summary generated."
    Write-Host "Summary report: $IndexPath"
    Write-Host "============================================================"

    if ($OpenReport -and (Test-Path -LiteralPath $IndexPath)) {
        Start-Process -FilePath $IndexPath
    }

    return [pscustomobject]@{
        ReportDir = $OutDir
        IndexPath = $IndexPath
        SummaryPath = Join-Path $OutDir "summary.json"
    }
}

$ExitCodes = @()
$RunResults = @()

$Run = Invoke-CompareSuite $Suite
$RunResults += $Run
$ExitCodes += $Run.ExitCode

if ($StrictExit) {
    foreach ($Code in $ExitCodes) {
        if ($Code -ne 0) {
            exit $Code
        }
    }
}

exit 0
