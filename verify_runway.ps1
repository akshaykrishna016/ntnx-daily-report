# ===========================================================================
#  Siemens - verify the CPU / Memory / Storage runway attribute names
#
#  The v3 groups API rejects the whole request if any attribute is unknown,
#  so this tests each candidate name in its own call and reports which are
#  real AND carry data. Read-only.
#
#  RUN: paste into PowerShell, or:
#       powershell -ExecutionPolicy Bypass -File .\verify_runway.ps1
#  Then tell me which lines print GREEN ("VALID + DATA") and their sample value.
# ===========================================================================

$pcHost = Read-Host "Prism Central host / IP"
$user   = Read-Host "PC username"
$sec    = Read-Host "PC password" -AsSecureString
$pass   = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
$v3     = "https://${pcHost}:9440/api/nutanix/v3"
$tmp    = Join-Path $env:TEMP "_runway_body.json"

# Candidate runway attribute names on the 'cluster' groups entity.
$cands = @(
  "capacity.runway",
  "capacity.cpu_runway",
  "capacity.memory_runway",
  "capacity.storage_runway",
  "capacity.cpu_runway_days",
  "capacity.memory_runway_days",
  "capacity.storage_runway_days",
  "capacity.runway_days",
  "capacity.overall_runway_days"
)

Write-Host "`nProbing runway attributes on entity_type 'cluster'...`n" -ForegroundColor Cyan

foreach ($a in $cands) {
  $body = '{"entity_type":"cluster","group_member_count":50,"group_member_offset":0,"group_member_attributes":[{"attribute":"cluster_name"},{"attribute":"' + $a + '"}]}'
  Set-Content -Path $tmp -Value $body -Encoding ascii -NoNewline
  $resp = curl.exe -k -s -u "${user}:${pass}" -H "Content-Type: application/json" -X POST --data "@$tmp" "$v3/groups"

  if ($resp -like '*Invalid Argument*') {
    Write-Host ("  INVALID       : {0}" -f $a) -ForegroundColor DarkGray
    continue
  }
  $sample = $null; $cnt = 0
  try {
    $j = $resp | ConvertFrom-Json
    foreach ($e in $j.group_results[0].entity_results) {
      $rec = $e.data | Where-Object { $_.name -eq $a }
      $inner = @(@($rec.values)[0].values)
      if ($inner.Count -gt 0 -and "$($inner[0])" -ne "") {
        $cnt++; if (-not $sample) { $sample = $inner[0] }
      }
    }
  } catch { }

  if ($cnt -gt 0) {
    Write-Host ("  VALID + DATA  : {0}   ({1} clusters, e.g. {2} days)" -f $a, $cnt, $sample) -ForegroundColor Green
  } else {
    Write-Host ("  valid, empty  : {0}" -f $a) -ForegroundColor Yellow
  }
}

Remove-Item $tmp -ErrorAction SilentlyContinue
Write-Host "`nSend me the GREEN lines (the real attribute names + sample values)." -ForegroundColor Green
