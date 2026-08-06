#Requires -Version 5.1
<#
.SYNOPSIS
    Deterministic static validation for the Equilibra build.

.DESCRIPTION
    Runs the build script twice, compares the dist/ checksums byte-for-byte,
    and verifies:
        - JSON parse + JSON-LD parse + JSON-LD type checks
        - Exactly one canonical/title/description in dist/index.html
        - FAQ HTML <-> JSON-LD parity (visible answer text == schema text)
        - dist/index.html, dist/robots.txt, dist/sitemap.xml exist
        - No copy of the template in dist/, no dead assets
        - No og:image / twitter:image while content/site.json og_image is null
        - Asset references in dist/index.html resolve to existing files

    Exits non-zero on any failure.
#>

[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$DistRoot,
    [string]$BuildScript
)

# Resolve defaults relative to the script's own location.
if (-not $PSBoundParameters.ContainsKey('SourceRoot')) {
    $SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
if (-not $PSBoundParameters.ContainsKey('DistRoot')) {
    $DistRoot = Join-Path $SourceRoot 'dist'
}
if (-not $PSBoundParameters.ContainsKey('BuildScript')) {
    $BuildScript = Join-Path $PSScriptRoot 'build.ps1'
}

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Info   { param($m) Write-Host "[verify] $m" }
function Write-Ok     { param($m) Write-Host "[verify] OK: $m" -ForegroundColor Green }
function Fail-Verify  {
    param([Parameter(Mandatory)][string]$m)
    Write-Host "[verify] FATAL: $m" -ForegroundColor Red
    exit 1
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Path)
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try { $bytes = $hash.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $hash.Dispose() }
    ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

function Get-ChecksumSnapshot {
    param([Parameter(Mandatory)][string]$Root)
    $rootFull = (Resolve-Path $Root).Path
    $map = @{}
    Get-ChildItem -LiteralPath $Root -File -Recurse | ForEach-Object {
        $rel = $_.FullName.Substring($rootFull.Length).TrimStart('\','/').Replace('\','/')
        $map[$rel] = @{ Sha = (Get-Sha256Hex $_.FullName); Size = $_.Length }
    }
    return $map
}

# 1. Run the build twice and compare the resulting dist/
Write-Info "Running build #1..."
& $BuildScript -SourceRoot $SourceRoot -DistRoot $DistRoot | Out-Null
$snap1 = Get-ChecksumSnapshot $DistRoot
Write-Info "Build #1 produced $(($snap1.Keys).Count) files."

Write-Info "Running build #2..."
& $BuildScript -SourceRoot $SourceRoot -DistRoot $DistRoot | Out-Null
$snap2 = Get-ChecksumSnapshot $DistRoot
Write-Info "Build #2 produced $(($snap2.Keys).Count) files."

if ($snap1.Keys.Count -ne $snap2.Keys.Count) {
    Fail-Verify "Determinism: file count differs between runs ($($snap1.Keys.Count) vs $($snap2.Keys.Count))."
}
foreach ($k in $snap1.Keys) {
    if (-not $snap2.ContainsKey($k)) { Fail-Verify "Determinism: file '$k' present in run #1 but not run #2." }
    if ($snap1[$k].Sha -ne $snap2[$k].Sha) { Fail-Verify "Determinism: SHA-256 differs for '$k' between runs." }
    if ($snap1[$k].Size -ne $snap2[$k].Size) { Fail-Verify "Determinism: byte size differs for '$k' between runs." }
}
Write-Ok "Determinism: dist/ is byte-identical across two consecutive builds."

# 2. Required dist files
foreach ($f in @('index.html','robots.txt','sitemap.xml','checksums.sha256','build-manifest.json')) {
    $p = Join-Path $DistRoot $f
    if (-not (Test-Path -LiteralPath $p)) { Fail-Verify "Required dist file missing: $f" }
}
Write-Ok "Required dist files present."

# 3. Forbidden dist files
foreach ($f in @('equilibra.html','template.html','isologo.png','logo-trANSPARENCIA.png')) {
    $p = Join-Path $DistRoot $f
    if (Test-Path -LiteralPath $p) { Fail-Verify "Forbidden dist file present: $f" }
}
Write-Ok "No forbidden files in dist/."

# 4. Parse JSON inputs
$siteJsonPath = Join-Path $SourceRoot 'content/site.json'
$faqsJsonPath = Join-Path $SourceRoot 'content/faqs.json'
try { $site = [System.IO.File]::ReadAllText($siteJsonPath, [System.Text.UTF8Encoding]::new($false)) | ConvertFrom-Json } catch { Fail-Verify "content/site.json parse error: $($_.Exception.Message)" }
try { $faqs = [System.IO.File]::ReadAllText($faqsJsonPath, [System.Text.UTF8Encoding]::new($false)) | ConvertFrom-Json } catch { Fail-Verify "content/faqs.json parse error: $($_.Exception.Message)" }
Write-Ok "JSON sources parse."

# 5. Parse and validate JSON-LD
$indexPath = Join-Path $DistRoot 'index.html'
$indexHtml = [System.IO.File]::ReadAllText($indexPath, [System.Text.UTF8Encoding]::new($false))
$jsonLdPattern = '(?s)<script type="application/ld\+json">\s*(\{.*?\})\s*</script>'
$jsonLdMatches = [regex]::Matches($indexHtml, $jsonLdPattern)
if ($jsonLdMatches.Count -ne 1) { Fail-Verify "JSON-LD: expected exactly one <script type='application/ld+json'> in dist/index.html, found $($jsonLdMatches.Count)." }
try { $jsonLd = $jsonLdMatches[0].Groups[1].Value | ConvertFrom-Json } catch { Fail-Verify "JSON-LD: parse error: $($_.Exception.Message)" }
if ($jsonLd.'@context' -ne 'https://schema.org') { Fail-Verify "JSON-LD: missing or wrong @context." }
if ($null -eq $jsonLd.'@graph') { Fail-Verify "JSON-LD: missing @graph." }
$entityTypes = @()
foreach ($node in $jsonLd.'@graph') { $entityTypes += [string]$node.'@type' }
$entityTypes = $entityTypes | Sort-Object -Unique
if (($entityTypes -contains 'MedicalBusiness') -ne $true)  { Fail-Verify "JSON-LD: missing MedicalBusiness entity." }
if (($entityTypes -contains 'FAQPage') -ne $true)         { Fail-Verify "JSON-LD: missing FAQPage entity." }
if (($entityTypes -contains 'Service') -ne $true)         { Fail-Verify "JSON-LD: missing Service entity." }
Write-Ok "JSON-LD types present: $($entityTypes -join ', ')."

# 6. Canonical / title / description parity (single instance, no dupe)
$canonicalCount = ([regex]::Matches($indexHtml, 'rel=["'']canonical["'']')).Count
$titleCount     = ([regex]::Matches($indexHtml, '<title>')).Count
$descCount      = ([regex]::Matches($indexHtml, 'name=["'']description["'']')).Count
if ($canonicalCount -ne 1) { Fail-Verify "Exactly one <link rel='canonical'> expected, found $canonicalCount." }
if ($titleCount     -ne 1) { Fail-Verify "Exactly one <title> expected, found $titleCount." }
if ($descCount      -ne 1) { Fail-Verify "Exactly one meta name='description' expected, found $descCount." }
Write-Ok "Canonical/title/description single-instance check."

# 7. FAQ HTML <-> JSON-LD parity
$faqEntity = $jsonLd.'@graph' | Where-Object { $_.PSObject.Properties.Match('mainEntity').Count -gt 0 } | Select-Object -First 1
if ($null -eq $faqEntity) { Fail-Verify "JSON-LD: FAQPage entity with mainEntity not found." }
$schemaAnswers = @()
foreach ($q in $faqEntity.mainEntity) { $schemaAnswers += [string]$q.acceptedAnswer.text }
$renderedPattern = '(?s)<details>\s*<summary>[^<]*</summary>\s*<p>([^<]*)</p>\s*</details>'
$renderedAnswers = @()
foreach ($m in [regex]::Matches($indexHtml, $renderedPattern)) { $renderedAnswers += ($m.Groups[1].Value).Trim() }
if ($renderedAnswers.Count -ne $schemaAnswers.Count) { Fail-Verify "FAQ parity: rendered=$($renderedAnswers.Count) schema=$($schemaAnswers.Count)." }
for ($i = 0; $i -lt $renderedAnswers.Count; $i++) {
    if ($renderedAnswers[$i] -ne $schemaAnswers[$i]) { Fail-Verify "FAQ parity: answer $i differs between rendered HTML and JSON-LD." }
}
Write-Ok "FAQ parity: $($renderedAnswers.Count) Q/A pairs identical in HTML and JSON-LD."

# 8. Asset reference resolution
$refPattern = '(?:href|src)\s*=\s*"(?!https?://|data:|#|mailto:|tel:)([^"]+)"'
$broken = @()
foreach ($m in [regex]::Matches($indexHtml, $refPattern)) {
    $rel = $m.Groups[1].Value
    if ($rel.StartsWith('/')) { $rel = $rel.TrimStart('/') }
    $p = Join-Path $DistRoot $rel
    if (-not (Test-Path -LiteralPath $p)) { $broken += $rel }
}
if ($broken.Count -gt 0) { Fail-Verify "Broken asset references: $($broken -join ', ')" }
Write-Ok "All asset references resolve to existing dist/ files."

# 9. OG image policy
if ($null -eq $site.og_image) {
    if ($indexHtml -match 'property=["'']og:image["'']')    { Fail-Verify "og:image present in dist/index.html but content/site.json og_image is null." }
    if ($indexHtml -match 'name=["'']twitter:image["'']')   { Fail-Verify "twitter:image present in dist/index.html but content/site.json og_image is null." }
    Write-Ok "OG image policy: no og:image / twitter:image while og_image is null."
} else {
    Write-Info "og_image is set to '$($site.og_image)'; skipping null-policy check."
}

# 10. robots.txt + sitemap.xml content
$robotsTxt = [System.IO.File]::ReadAllText((Join-Path $DistRoot 'robots.txt'), [System.Text.UTF8Encoding]::new($false))
if ($robotsTxt -notmatch 'User-agent:\s*\*')    { Fail-Verify "robots.txt missing 'User-agent: *'." }
if ($robotsTxt -notmatch 'Allow:\s*/')          { Fail-Verify "robots.txt missing 'Allow: /'." }
if ($robotsTxt -notmatch 'Sitemap:')            { Fail-Verify "robots.txt missing Sitemap directive." }
Write-Ok "robots.txt content valid."
$sitemapXml = [System.IO.File]::ReadAllText((Join-Path $DistRoot 'sitemap.xml'), [System.Text.UTF8Encoding]::new($false))
if ($sitemapXml -notmatch '<urlset') { Fail-Verify "sitemap.xml missing <urlset>." }
if ($sitemapXml -notmatch ([regex]::Escape([string]$site.site_url))) { Fail-Verify "sitemap.xml does not reference site_url." }
Write-Ok "sitemap.xml content valid."

Write-Info "All verification checks passed."
