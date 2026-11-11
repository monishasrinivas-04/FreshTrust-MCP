$ErrorActionPreference = 'Stop'

# Repository to test
$repo = 'Kalpana-Nayaka/project_rag'

# Ask securely for the same fine-grained PAT entered in FreshTrust.
$securePat = Read-Host 'Enter the same fine-grained PAT used in FreshTrust' -AsSecureString
$bstr = [IntPtr]::Zero
$pat = $null
$headers = $null

function Invoke-GitHubCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][hashtable]$Headers
    )

    try {
        $data = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -ErrorAction Stop
        Write-Host ("{0}: SUCCESS" -f $Name) -ForegroundColor Green

        return [pscustomobject]@{
            Success = $true
            Status  = 200
            Data    = $data
        }
    }
    catch {
        $status = 'Unknown'
        $response = $_.Exception.Response

        if ($null -ne $response -and $null -ne $response.StatusCode) {
            $status = [int]$response.StatusCode
        }

        Write-Host ("{0}: FAILED (HTTP {1})" -f $Name, $status) -ForegroundColor Red

        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Host ("GitHub response: {0}" -f $_.ErrorDetails.Message)
        }
        elseif ($status -eq 'Unknown') {
            Write-Host ("Request error: {0}" -f $_.Exception.Message)
        }

        return [pscustomobject]@{
            Success = $false
            Status  = $status
            Data    = $null
        }
    }
}

try {
    if ($null -eq $securePat -or $securePat.Length -eq 0) {
        throw 'No PAT was entered.'
    }

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePat)
    $pat = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    $headers = @{
        Authorization = "Bearer $pat"
        Accept = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent' = 'FreshTrust-MCP-Diagnostic'
    }

    Write-Host "`nTesting repository: $repo`n" -ForegroundColor Cyan

    $identity = Invoke-GitHubCheck `
        -Name 'PAT identity verification' `
        -Uri 'https://api.github.com/user' `
        -Headers $headers

    if ($identity.Success) {
        Write-Host ("Authenticated GitHub account: {0}" -f $identity.Data.login)
    }

    $repository = Invoke-GitHubCheck `
        -Name 'Private repository access' `
        -Uri "https://api.github.com/repos/$repo" `
        -Headers $headers

    if ($repository.Success) {
        Write-Host ("Repository returned by GitHub: {0}" -f $repository.Data.full_name)
        Write-Host ("Private repository: {0}" -f $repository.Data.private)
    }

    $emails = Invoke-GitHubCheck `
        -Name 'PAT email permission' `
        -Uri 'https://api.github.com/user/emails?per_page=100' `
        -Headers $headers

    if ($emails.Success) {
        $emailList = @($emails.Data)
        $verifiedEmails = @($emailList | Where-Object { $_.verified -eq $true })
        $primaryVerifiedEmails = @($verifiedEmails | Where-Object { $_.primary -eq $true })

        Write-Host ("Verified email count: {0}" -f $verifiedEmails.Count)
        Write-Host ("Primary verified email available: {0}" -f ($primaryVerifiedEmails.Count -gt 0))
        # Email addresses themselves are deliberately not displayed.
    }
}
finally {
    $pat = $null
    $headers = $null

    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
