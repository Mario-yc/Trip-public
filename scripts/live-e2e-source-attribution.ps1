function Test-LiveE2ESourceAttribution {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedGitCommit,

        [string[]]$RequiredSourcePaths = @()
    )

    $resolvedRepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    $expectedCommit = $ExpectedGitCommit.Trim().ToLowerInvariant()
    $rawObservedCommit = & git -C $resolvedRepoRoot rev-parse HEAD
    $commitExitCode = $LASTEXITCODE
    $observedCommit = ([string]$rawObservedCommit).Trim().ToLowerInvariant()

    $rawTrackedStatus = @(& git -C $resolvedRepoRoot status --porcelain=v1 --untracked-files=no)
    $statusExitCode = $LASTEXITCODE
    $trackedStatusText = [string]::Join("`n", $rawTrackedStatus).Trim()
    $trackedSourceClean = $statusExitCode -eq 0 -and -not $trackedStatusText
    $requiredSourceResults = @()
    foreach ($pathValue in @($RequiredSourcePaths | Where-Object { $_ -and $_.Trim() })) {
        $requiredSourceResults += Test-RequiredLiveE2ESource `
            -RepoRoot $resolvedRepoRoot `
            -ExpectedGitCommit $expectedCommit `
            -PathValue $pathValue
    }
    $requiredSourceFailure = $requiredSourceResults | Where-Object { -not $_.verified } | Select-Object -First 1

    $failureReason = $null
    if ($expectedCommit -notmatch '^[0-9a-f]{40}$') {
        $failureReason = "expected_git_commit_invalid"
    } elseif ($commitExitCode -ne 0) {
        $failureReason = "head_commit_unavailable"
    } elseif ($observedCommit -notmatch '^[0-9a-f]{40}$') {
        $failureReason = "observed_git_commit_invalid"
    } elseif ($observedCommit -ne $expectedCommit) {
        $failureReason = "head_commit_mismatch"
    } elseif ($statusExitCode -ne 0) {
        $failureReason = "tracked_source_status_unavailable"
    } elseif ($null -ne $requiredSourceFailure) {
        $failureReason = [string]$requiredSourceFailure.failureReason
    } elseif (-not $trackedSourceClean) {
        $failureReason = "tracked_source_dirty"
    }

    return [pscustomobject][ordered]@{
        schemaVersion = "trip-live-e2e-source-attribution-v1"
        verified = $null -eq $failureReason
        expectedGitCommit = $expectedCommit
        observedGitCommit = $observedCommit
        headMatches = $observedCommit -eq $expectedCommit
        trackedSourceClean = $trackedSourceClean
        trackedChangeCount = if ($trackedSourceClean) { 0 } else { @($rawTrackedStatus).Count }
        requiredSourcePaths = @($requiredSourceResults | ForEach-Object { $_.path })
        requiredSourceResults = @($requiredSourceResults)
        failureReason = $failureReason
    }
}

function Test-RequiredLiveE2ESource {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedGitCommit,

        [Parameter(Mandatory = $true)]
        [string]$PathValue
    )

    $normalizedPath = Convert-ToGitRelativeSourcePath -RepoRoot $RepoRoot -PathValue $PathValue
    $absolutePath = Join-Path $RepoRoot ($normalizedPath -replace '/', '\')
    $existsOnDisk = Test-Path -LiteralPath $absolutePath -PathType Leaf
    $tracked = $false
    if ($existsOnDisk) {
        & git -C $RepoRoot ls-files --error-unmatch -- $normalizedPath 2>$null | Out-Null
        $tracked = $LASTEXITCODE -eq 0
    }
    $presentInExpectedCommit = $false
    if ($tracked -and $ExpectedGitCommit -match '^[0-9a-f]{40}$') {
        $gitObjectSpec = '{0}:{1}' -f $ExpectedGitCommit, $normalizedPath
        & git -C $RepoRoot cat-file -e $gitObjectSpec 2>$null
        $presentInExpectedCommit = $LASTEXITCODE -eq 0
    }

    $failureReason = $null
    if (-not $existsOnDisk) {
        $failureReason = "required_source_missing"
    } elseif (-not $tracked) {
        $failureReason = "required_source_untracked"
    } elseif (-not $presentInExpectedCommit) {
        $failureReason = "required_source_missing_from_frozen_commit"
    }

    return [pscustomobject][ordered]@{
        path = $normalizedPath
        existsOnDisk = $existsOnDisk
        tracked = $tracked
        presentInExpectedCommit = $presentInExpectedCommit
        verified = $null -eq $failureReason
        failureReason = $failureReason
    }
}

function Convert-ToGitRelativeSourcePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot,

        [Parameter(Mandatory = $true)]
        [string]$PathValue
    )

    $resolvedRepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    # Windows PowerShell 5.1 runs on .NET Framework, which does not expose
    # Path.TrimEndingDirectorySeparator. Keep the live gate compatible with
    # both Windows PowerShell and modern pwsh without changing path semantics.
    $directorySeparators = [char[]]@(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $repoPrefix = $resolvedRepoRoot.TrimEnd($directorySeparators)
    $repoPrefixWithSeparator = $repoPrefix + [IO.Path]::DirectorySeparatorChar
    $candidateFullPath = if ([IO.Path]::IsPathRooted($PathValue)) {
        [IO.Path]::GetFullPath($PathValue)
    } else {
        [IO.Path]::GetFullPath((Join-Path $resolvedRepoRoot $PathValue))
    }
    if (-not $candidateFullPath.StartsWith($repoPrefixWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Required source path must stay within the repository: $PathValue"
    }
    $relativePath = $candidateFullPath.Substring($repoPrefix.Length).TrimStart('\', '/')
    if (-not $relativePath) {
        throw "Required source path must not resolve to the repository root"
    }
    return $relativePath -replace '\\', '/'
}
