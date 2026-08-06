#Requires -Version 5.1
<#
.SYNOPSIS
    Deterministically build the Equilibra static site into dist/.

.DESCRIPTION
    Reads:
        content/site.json    (canonical SEO/entity values)
        content/faqs.json    (approved FAQ Q&A)
        src/template.html    (sole HTML template with BUILD markers)
        tracked asset files  (svg, png, ico, apple-touch-icon)
    Writes:
        dist/index.html
        dist/sitemap.xml
        dist/robots.txt
        dist/<assets> (only those referenced from the template)
        dist/build-manifest.json (internal build manifest, NOT a PWA manifest)
        dist/checksums.sha256 (sorted `SHA256  bytes  relative-path`, UTF-8 LF)

    The script is fully deterministic: running it twice with unchanged inputs
    produces byte-identical dist/. It fails loudly on:
        - missing or duplicate BUILD markers in the template
        - JSON parse errors
        - FAQ schema text mismatch (visible HTML vs JSON-LD)
        - any og:image/twitter:image while content/site.json og_image is null
        - a populated og_image but the referenced asset does not exist
        - forbidden dist files (the template itself, dead assets)
#>

[CmdletBinding()]
param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$DistRoot   = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'dist')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Info { param($m) Write-Host "[build] $m" }

function Fail-Build {
    param([Parameter(Mandatory)][string]$m)
    Write-Host "[build] FATAL: $m" -ForegroundColor Red
    exit 1
}

function Write-LfUtf8 {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Content
    )
    $normalized = ($Content -replace "`r`n", "`n") -replace "`r", "`n"
    [System.IO.File]::WriteAllText($Path, $normalized, [System.Text.UTF8Encoding]::new($false))
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

# 1. Validate inputs
$templatePath = Join-Path $SourceRoot 'src/template.html'
$siteJsonPath = Join-Path $SourceRoot 'content/site.json'
$faqsJsonPath = Join-Path $SourceRoot 'content/faqs.json'
foreach ($p in @($templatePath, $siteJsonPath, $faqsJsonPath)) {
    if (-not (Test-Path -LiteralPath $p)) { Fail-Build "Required input not found: $p" }
}
$template = [System.IO.File]::ReadAllText($templatePath, [System.Text.UTF8Encoding]::new($false))

# 2. Validate BUILD markers (each exactly once, end after start)
$markerPairs = @(
    @{ Start = '<!--BUILD:JSONLD_START-->'; End = '<!--BUILD:JSONLD_END-->'; Name = 'JSONLD' },
    @{ Start = '<!--BUILD:FAQ_HTML_START-->'; End = '<!--BUILD:FAQ_HTML_END-->'; Name = 'FAQ_HTML' }
)
foreach ($m in $markerPairs) {
    $startCount = ([regex]::Matches($template, [regex]::Escape($m.Start))).Count
    $endCount   = ([regex]::Matches($template, [regex]::Escape($m.End))).Count
    if ($startCount -ne 1) { Fail-Build ("Marker {0} appears {1} times (expected exactly 1)" -f $m.Start, $startCount) }
    if ($endCount   -ne 1) { Fail-Build ("Marker {0} appears {1} times (expected exactly 1)" -f $m.End,   $endCount) }
    $startIdx = $template.IndexOf($m.Start, [StringComparison]::Ordinal)
    $endIdx   = $template.IndexOf($m.End,   [StringComparison]::Ordinal)
    if ($endIdx -le $startIdx) { Fail-Build ("Marker pair {0} is malformed (end precedes start)" -f $m.Name) }
}

# 3. Parse JSON sources
try {
    $siteJsonRaw = [System.IO.File]::ReadAllText($siteJsonPath, [System.Text.UTF8Encoding]::new($false))
    $site        = $siteJsonRaw | ConvertFrom-Json
} catch { Fail-Build "Failed to parse content/site.json as JSON: $($_.Exception.Message)" }
try {
    $faqsJsonRaw = [System.IO.File]::ReadAllText($faqsJsonPath, [System.Text.UTF8Encoding]::new($false))
    $faqs        = $faqsJsonRaw | ConvertFrom-Json
} catch { Fail-Build "Failed to parse content/faqs.json as JSON: $($_.Exception.Message)" }

# 4. Validate site.json fields (no fabrication).
# Note: 'og_image' is allowed to be JSON null; we only require the property to
# exist. Other required fields must be non-null.
$requiredSiteFields = @('site_name','site_url','locale','title','description','entity_statement','telephone','area_served','services')
foreach ($f in $requiredSiteFields) {
    if ($null -eq $site.$f) { Fail-Build "content/site.json is missing required field: $f" }
}
if (-not ($site.PSObject.Properties.Name -contains 'og_image')) {
    Fail-Build "content/site.json is missing required field: og_image"
}
if ($null -ne $site.og_image) {
    $ogAssetPath = Join-Path $SourceRoot $site.og_image
    if (-not (Test-Path -LiteralPath $ogAssetPath)) {
        Fail-Build "content/site.json og_image is set to '$($site.og_image)' but the asset does not exist on disk."
    }
}

# 5. Validate FAQ array
if ($null -eq $faqs.items -or $faqs.items.Count -eq 0) {
    Fail-Build "content/faqs.json is missing or empty (expected a non-empty 'items' array)."
}
$expectedFaqCount = $faqs.items.Count
Write-Info "FAQ count: $expectedFaqCount"

# 6. Build visible FAQ HTML; record answers for parity check
$faqAnswerByIndex = New-Object System.Collections.Generic.List[string]
$faqDetails = New-Object System.Collections.Generic.List[string]
foreach ($entry in $faqs.items) {
    if ($null -eq $entry.question -or $null -eq $entry.answer) { Fail-Build "FAQ item missing question or answer" }
    $q = ([string]$entry.question).Trim()
    $a = ([string]$entry.answer).Trim()
    if ($q.Length -eq 0 -or $a.Length -eq 0) { Fail-Build "FAQ item has empty question or answer" }
    $faqAnswerByIndex.Add($a)
    $qEsc = ($q -replace '&','&amp;') -replace '<','&lt;' -replace '>','&gt;'
    $aEsc = ($a -replace '&','&amp;') -replace '<','&lt;' -replace '>','&gt;'
    $faqDetails.Add("  <details><summary>$qEsc</summary><p>$aEsc</p></details>")
}
$faqHtml = ($faqDetails -join "`n") + "`n"

# 7. Compose JSON-LD @graph
function Escape-JsonString {
    param([Parameter(Mandatory)][string]$s)
    $sb = New-Object System.Text.StringBuilder
    foreach ($c in $s.ToCharArray()) {
        switch -CaseSensitive ($c) {
            '"'  { [void]$sb.Append('\"') }
            '\\' { [void]$sb.Append('\\\\') }
            "`b" { [void]$sb.Append('\b') }
            "`f" { [void]$sb.Append('\f') }
            "`n" { [void]$sb.Append('\n') }
            "`r" { [void]$sb.Append('\r') }
            "`t" { [void]$sb.Append('\t') }
            default {
                if ([int]$c -lt 0x20) { [void]$sb.AppendFormat('\u{0:x4}', [int]$c) }
                else { [void]$sb.Append($c) }
            }
        }
    }
    return $sb.ToString()
}
function Make-JsonString {
    param([Parameter(Mandatory)][string]$s)
    '"' + (Escape-JsonString $s) + '"'
}

# areaServed array
$areaParts = New-Object System.Collections.Generic.List[string]
foreach ($a in $site.area_served) {
    $inner = New-Object System.Collections.Generic.List[string]
    [void]$inner.Add( (Make-JsonString '@type') + ':' + (Make-JsonString 'AdministrativeArea') )
    [void]$inner.Add( (Make-JsonString 'name')  + ':' + (Make-JsonString ([string]$a.name)) )
    if ($null -ne $a.containedInPlace) {
        $cp = $a.containedInPlace
        $cpInner = New-Object System.Collections.Generic.List[string]
        [void]$cpInner.Add( (Make-JsonString '@type') + ':' + (Make-JsonString ([string]$cp.type)) )
        [void]$cpInner.Add( (Make-JsonString 'name')  + ':' + (Make-JsonString ([string]$cp.name)) )
        if ($null -ne $cp.addressCountry) { [void]$cpInner.Add( (Make-JsonString 'addressCountry') + ':' + (Make-JsonString ([string]$cp.addressCountry)) ) }
        [void]$inner.Add( (Make-JsonString 'containedInPlace') + ':{' + (($cpInner -join ',')) + '}' )
    }
    $areaParts.Add('{' + (($inner -join ',')) + '}')
}
$areaServedJson = '[' + (($areaParts -join ',')) + ']'

# MedicalBusiness entity
$medicalEntityParts = New-Object System.Collections.Generic.List[string]
[void]$medicalEntityParts.Add( (Make-JsonString '@context')     + ':' + (Make-JsonString 'https://schema.org') )
[void]$medicalEntityParts.Add( (Make-JsonString '@type')        + ':' + (Make-JsonString 'MedicalBusiness') )
[void]$medicalEntityParts.Add( (Make-JsonString 'name')         + ':' + (Make-JsonString ([string]$site.site_name)) )
[void]$medicalEntityParts.Add( (Make-JsonString 'url')          + ':' + (Make-JsonString ([string]$site.site_url)) )
[void]$medicalEntityParts.Add( (Make-JsonString 'telephone')    + ':' + (Make-JsonString ([string]$site.telephone)) )
[void]$medicalEntityParts.Add( (Make-JsonString 'areaServed')   + ':' + $areaServedJson )
[void]$medicalEntityParts.Add( (Make-JsonString 'description')  + ':' + (Make-JsonString ([string]$site.entity_statement)) )

# Service entities
$serviceEntities = New-Object System.Collections.Generic.List[string]
foreach ($s in $site.services) {
    $parts = New-Object System.Collections.Generic.List[string]
    [void]$parts.Add( (Make-JsonString '@context')    + ':' + (Make-JsonString 'https://schema.org') )
    [void]$parts.Add( (Make-JsonString '@type')       + ':' + (Make-JsonString 'Service') )
    [void]$parts.Add( (Make-JsonString 'name')        + ':' + (Make-JsonString ([string]$s.name)) )
    [void]$parts.Add( (Make-JsonString 'serviceType') + ':' + (Make-JsonString ([string]$s.name)) )
    [void]$parts.Add( (Make-JsonString 'description') + ':' + (Make-JsonString ([string]$s.description)) )
    [void]$parts.Add( (Make-JsonString 'provider')    + ':' + (Make-JsonString ([string]$site.site_url)) )
    [void]$parts.Add( (Make-JsonString 'areaServed')  + ':' + $areaServedJson )
    $serviceEntities.Add('{' + (($parts -join ',')) + '}')
}

# FAQPage entity
$faqItemsParts = New-Object System.Collections.Generic.List[string]
$commaSep = ','
for ($i = 0; $i -lt $expectedFaqCount; $i++) {
    $q = ([string]$faqs.items[$i].question).Trim()
    $a = ([string]$faqs.items[$i].answer).Trim()
    # Build the inner acceptedAnswer as a single string so commas don't get
    # interpreted as array separators.
    $innerAns = (Make-JsonString '@type') + ':' + (Make-JsonString 'Answer') + $commaSep + (Make-JsonString 'text') + ':' + (Make-JsonString $a)
    $itemParts = @(
        (Make-JsonString '@type')          + ':' + (Make-JsonString 'Question')
        (Make-JsonString 'name')           + ':' + (Make-JsonString $q)
        (Make-JsonString 'acceptedAnswer') + ':{' + $innerAns + '}'
    )
    $itemStr = $itemParts -join $commaSep
    $faqItemsParts.Add('{' + $itemStr + '}')
}
$faqEntityParts = New-Object System.Collections.Generic.List[string]
[void]$faqEntityParts.Add( (Make-JsonString '@context')   + ':' + (Make-JsonString 'https://schema.org') )
[void]$faqEntityParts.Add( (Make-JsonString '@type')      + ':' + (Make-JsonString 'FAQPage') )
[void]$faqEntityParts.Add( (Make-JsonString 'mainEntity') + ':[' + (($faqItemsParts -join ',')) + ']' )

# @graph
$graphItems = New-Object System.Collections.Generic.List[string]
[void]$graphItems.Add('{' + (($medicalEntityParts -join ',')) + '}')
foreach ($s in $serviceEntities) { [void]$graphItems.Add($s) }
[void]$graphItems.Add('{' + (($faqEntityParts -join ',')) + '}')

$jsonLdObject  = '{' + (Make-JsonString '@context') + ':' + (Make-JsonString 'https://schema.org') + ',' + (Make-JsonString '@graph') + ':[' + (($graphItems -join ',')) + ']}'
$jsonLdScript  = '<script type="application/ld+json">' + "`n" + $jsonLdObject + "`n" + '</script>'

# 8. Reject any og:image/twitter:image in template while og_image is null
if ($null -eq $site.og_image) {
    if ($template -match 'property=["'']og:image["'']')    { Fail-Build "Template contains og:image meta but content/site.json og_image is null." }
    if ($template -match 'name=["'']twitter:image["'']')   { Fail-Build "Template contains twitter:image meta but content/site.json og_image is null." }
}

# 9. Inject JSON-LD region
$jsonldStartIdx = $template.IndexOf('<!--BUILD:JSONLD_START-->', [StringComparison]::Ordinal)
$jsonldEndIdx   = $template.IndexOf('<!--BUILD:JSONLD_END-->',   [StringComparison]::Ordinal)
$jsonldNewRegion = '<!--BUILD:JSONLD_START-->' + "`n" + $jsonLdScript + "`n" + '<!--BUILD:JSONLD_END-->'
$template = $template.Substring(0, $jsonldStartIdx) + $jsonldNewRegion + $template.Substring($jsonldEndIdx + '<!--BUILD:JSONLD_END-->'.Length)

# 10. Inject FAQ HTML region (replace the contents of `<div class="faq">...</div>`)
$divOpenIdx  = $template.IndexOf('<div class="faq">', [StringComparison]::Ordinal)
if ($divOpenIdx -lt 0) { Fail-Build "Could not locate <div class=""faq""> in the template." }
$divCloseIdx = $template.IndexOf('</div>', $divOpenIdx, [StringComparison]::Ordinal)
if ($divCloseIdx -lt 0) { Fail-Build "Could not locate closing </div> after <div class=""faq""> in the template." }
$faqBlockNew = '<div class="faq">' + "`n" + $faqHtml + ' </div>'
$template = $template.Substring(0, $divOpenIdx) + $faqBlockNew + $template.Substring($divCloseIdx + '</div>'.Length)

# 11. Parity check: rendered FAQ answers vs JSON-LD answers
$renderedAnswers = New-Object System.Collections.Generic.List[string]
$renderedPattern = '(?s)<details>\s*<summary>[^<]*</summary>\s*<p>([^<]*)</p>\s*</details>'
$renderedMatches = [regex]::Matches($template, $renderedPattern)
foreach ($m in $renderedMatches) { $renderedAnswers.Add( ($m.Groups[1].Value).Trim() ) }
if ($renderedAnswers.Count -ne $expectedFaqCount) {
    Fail-Build "FAQ parity: rendered HTML has $(([regex]::Matches($template,'<details>')).Count) <details> items but JSON has $expectedFaqCount"
}
for ($i = 0; $i -lt $expectedFaqCount; $i++) {
    if ($renderedAnswers[$i] -ne $faqAnswerByIndex[$i]) {
        Fail-Build "FAQ parity: item $i rendered answer does not match JSON-LD acceptedAnswer text."
    }
}

# 12. Reset dist/ and write dist/index.html
if (Test-Path -LiteralPath $DistRoot) { Remove-Item -LiteralPath $DistRoot -Recurse -Force }
New-Item -ItemType Directory -Path $DistRoot | Out-Null
$indexPath = Join-Path $DistRoot 'index.html'
Write-LfUtf8 -Path $indexPath -Content $template

# 13. Copy referenced static assets (allowlist by extension)
$allowedExts = @('.svg','.png','.ico','.jpg','.jpeg','.webp','.css','.js','.woff','.woff2','.txt','.xml','.json','.webmanifest')
$refPattern = '(?:href|src)\s*=\s*"(?!https?://|data:|#|mailto:|tel:)([^"]+)"'
$refMatches = [regex]::Matches($template, $refPattern)
$referenced = New-Object System.Collections.Generic.HashSet[string]
foreach ($m in $refMatches) {
    $rel = $m.Groups[1].Value
    if ($rel.StartsWith('/')) { $rel = $rel.TrimStart('/') }
    $referenced.Add($rel) | Out-Null
}
$referenced.Add('sitemap.xml') | Out-Null
$referenced.Add('robots.txt')  | Out-Null
$copiedFiles = New-Object System.Collections.Generic.List[object]
foreach ($rel in $referenced) {
    $srcPath = Join-Path $SourceRoot $rel
    if (-not (Test-Path -LiteralPath $srcPath)) {
        if ($rel -in @('sitemap.xml','robots.txt')) { continue }
        Fail-Build "Referenced asset not found in source: $rel"
    }
    $ext = [System.IO.Path]::GetExtension($rel).ToLowerInvariant()
    if ($allowedExts -notcontains $ext) { Fail-Build "Disallowed asset extension in dist: $rel" }
    $destPath = Join-Path $DistRoot $rel
    $destDir  = Split-Path -Path $destPath -Parent
    if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    Copy-Item -LiteralPath $srcPath -Destination $destPath -Force
    $copiedFiles.Add([pscustomobject]@{ Rel = $rel; Size = (Get-Item -LiteralPath $destPath).Length })
}
# equilibra.html is kept in the list on purpose: the template used to live at the
# repo root under that name and a past deploy published it. The guard costs nothing
# and still catches a stale copy reappearing.
$forbidden = @('isologo.png', 'logo-trANSPARENCIA.png', 'equilibra.html', 'template.html')
foreach ($f in $forbidden) {
    $p = Join-Path $DistRoot $f
    if (Test-Path -LiteralPath $p) { Fail-Build "Forbidden file present in dist/: $f" }
}

# 14. sitemap.xml
$sitemapEntries = New-Object System.Collections.Generic.List[string]
$sitemapEntries.Add( ('  <url><loc>{0}</loc></url>' -f ([string]$site.site_url)) )
$sitemapXml = @(
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
) + $sitemapEntries + @(
    '</urlset>'
) -join "`n"
Write-LfUtf8 -Path (Join-Path $DistRoot 'sitemap.xml') -Content $sitemapXml

# 15. robots.txt (allow all + Sitemap directive)
$robotsTxt = @(
    'User-agent: *'
    'Allow: /'
    ''
    ('Sitemap: {0}sitemap.xml' -f ([string]$site.site_url))
) -join "`n"
Write-LfUtf8 -Path (Join-Path $DistRoot 'robots.txt') -Content $robotsTxt

# 16. Internal build manifest (NOT a PWA manifest)
$manifestEntries = New-Object System.Collections.Generic.List[object]
foreach ($f in $copiedFiles) { $manifestEntries.Add([pscustomobject]@{ path = $f.Rel; bytes = $f.Size }) }
$buildManifest = [pscustomobject]@{
    schema            = 'equilibra-internal-build-manifest/v1'
    generated_at_utc  = 'DETERMINISTIC'
    source_root       = (Resolve-Path $SourceRoot).Path
    dist_root         = (Resolve-Path $DistRoot).Path
    template_bytes    = (Get-Item -LiteralPath $templatePath).Length
    site_json_sha256  = (Get-Sha256Hex $siteJsonPath)
    faqs_json_sha256  = (Get-Sha256Hex $faqsJsonPath)
    files             = $manifestEntries
}
$buildManifestJson = $buildManifest | ConvertTo-Json -Depth 6
Write-LfUtf8 -Path (Join-Path $DistRoot 'build-manifest.json') -Content $buildManifestJson

# 17. checksums.sha256 (sorted relative path)
# Capture file list first, then emit checksums (which include themselves only if
# we read the file back, but we exclude checksums.sha256 from its own digest to
# avoid a circular self-reference).
$distFiles = Get-ChildItem -LiteralPath $DistRoot -File -Recurse | ForEach-Object {
    $rel = $_.FullName.Substring((Resolve-Path $DistRoot).Path.Length).TrimStart('\','/').Replace('\','/')
    if ($rel -eq 'checksums.sha256') { return }  # exclude self
    [pscustomobject]@{ Rel = $rel; Size = $_.Length; Sha = (Get-Sha256Hex $_.FullName) }
} | Sort-Object Rel
$checksumLines = New-Object System.Collections.Generic.List[string]
foreach ($f in $distFiles) {
    $checksumLines.Add( ('{0}  {1}  {2}' -f $f.Sha, $f.Size.ToString().PadLeft(10), $f.Rel) )
}
$checksumsContent = ($checksumLines -join "`n") + "`n"
Write-LfUtf8 -Path (Join-Path $DistRoot 'checksums.sha256') -Content $checksumsContent

# Final report — recompute the actual dist/ file count (now includes checksums.sha256).
$finalFileCount = (Get-ChildItem -LiteralPath $DistRoot -File -Recurse).Count
$finalTotalBytes = (Get-ChildItem -LiteralPath $DistRoot -File -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Info ("dist/ written: {0} files, {1} bytes total" -f $finalFileCount, $finalTotalBytes)
Write-Info "Build complete."
