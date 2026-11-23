$ErrorActionPreference = "Stop"

$desktopRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $desktopRoot "build"
$probeName = "agentdu-appcontainer-probe-$([guid]::NewGuid().ToString('N'))"
$probeRoot = Join-Path $buildRoot $probeName
$protectedRoot = Join-Path $probeRoot "protected"
$resultRoot = Join-Path $probeRoot "results"
$protectedFile = Join-Path $protectedRoot "private.txt"
$outsideWrite = Join-Path $protectedRoot "outside-write.txt"
$childWrite = Join-Path $protectedRoot "child-write.txt"
$resultFile = Join-Path $resultRoot "probe-results.txt"
$profileName = "AgentDU.D1.$(([guid]::NewGuid().ToString('N')).Substring(0, 20))"
$profileCreated = $false
$tcpListener = $null

$resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
$resolvedProbeRoot = [System.IO.Path]::GetFullPath($probeRoot)
if (-not $resolvedProbeRoot.StartsWith($resolvedBuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a probe directory outside desktop/build."
}

New-Item -ItemType Directory -Path $protectedRoot -Force | Out-Null
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
Set-Content -LiteralPath $protectedFile -Value "private workspace data" -Encoding utf8

$resultAcl = [System.IO.Directory]::GetAccessControl($resultRoot)
$allPackagesSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-15-2-1")
$resultRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $allPackagesSid,
    [System.Security.AccessControl.FileSystemRights]::Modify,
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$resultAcl.SetAccessRule($resultRule)
[System.IO.Directory]::SetAccessControl($resultRoot, $resultAcl)

$nativeSource = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class AgentDuAppContainerProbe {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFOEX {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_CAPABILITIES {
        public IntPtr AppContainerSid;
        public IntPtr Capabilities;
        public uint CapabilityCount;
        public uint Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int CreateAppContainerProfile(string name, string displayName, string description, IntPtr capabilities, uint capabilityCount, out IntPtr appContainerSid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeriveAppContainerSidFromAppContainerName(string name, out IntPtr appContainerSid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeleteAppContainerProfile(string name);

    public static int DeleteProfile(string name) {
        return DeleteAppContainerProfile(name);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(IntPtr attributeList, int attributeCount, uint flags, ref IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(IntPtr attributeList, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previousValue, IntPtr returnSize);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CreateProcessW(string applicationName, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint creationFlags, IntPtr environment, string currentDirectory, ref STARTUPINFOEX startupInfo, out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DeleteProcThreadAttributeList(IntPtr attributeList);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint desiredAccess, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int informationClass, out int information, int informationLength, out int returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("advapi32.dll")]
    private static extern IntPtr FreeSid(IntPtr sid);

    public static bool IsProcessAppContainer(uint processId) {
        IntPtr process = OpenProcess(0x1000, false, processId);
        if (process == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "OpenProcess child");
        try {
            IntPtr token;
            Check(OpenProcessToken(process, 0x0008, out token), "OpenProcessToken child");
            try {
                int tokenIsAppContainer;
                int returned;
                Check(GetTokenInformation(token, 29, out tokenIsAppContainer, sizeof(int), out returned), "GetTokenInformation child");
                return tokenIsAppContainer != 0;
            } finally {
                CloseHandle(token);
            }
        } finally {
            CloseHandle(process);
        }
    }

    private static void Check(bool result, string operation) {
        if (!result) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), operation);
    }

    public static uint Run(string profileName, string executable, string encodedCommand, string currentDirectory, out bool isAppContainer) {
        IntPtr sid = IntPtr.Zero;
        IntPtr attributes = IntPtr.Zero;
        IntPtr securityCapabilities = IntPtr.Zero;
        PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
        try {
            int result = CreateAppContainerProfile(profileName, profileName, "Temporary AgentDU D1 isolation probe", IntPtr.Zero, 0, out sid);
            if (result == unchecked((int)0x800700B7)) {
                result = DeriveAppContainerSidFromAppContainerName(profileName, out sid);
                if (result != 0) Marshal.ThrowExceptionForHR(result);
            } else if (result != 0) {
                Marshal.ThrowExceptionForHR(result);
            }

            SECURITY_CAPABILITIES capabilities = new SECURITY_CAPABILITIES();
            capabilities.AppContainerSid = sid;
            securityCapabilities = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)));
            Marshal.StructureToPtr(capabilities, securityCapabilities, false);

            IntPtr attributeSize = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attributeSize);
            if (attributeSize == IntPtr.Zero) throw new InvalidOperationException("Windows did not report the process attribute list size.");
            attributes = Marshal.AllocHGlobal(attributeSize);
            Check(InitializeProcThreadAttributeList(attributes, 1, 0, ref attributeSize), "InitializeProcThreadAttributeList");
            Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x00020009), securityCapabilities, new IntPtr(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES))), IntPtr.Zero, IntPtr.Zero), "Set AppContainer security capabilities");

            STARTUPINFOEX startup = new STARTUPINFOEX();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            startup.StartupInfo.dwFlags = 1;
            startup.lpAttributeList = attributes;
            string command = "\"" + executable + "\" -NoProfile -NonInteractive -EncodedCommand " + encodedCommand;
            Check(CreateProcessW(executable, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero, false, 0x00080000 | 0x08000000, IntPtr.Zero, currentDirectory, ref startup, out processInfo), "CreateProcessW AppContainer");

            IntPtr token;
            Check(OpenProcessToken(processInfo.hProcess, 0x0008, out token), "OpenProcessToken");
            try {
                int tokenIsAppContainer;
                int returned;
                Check(GetTokenInformation(token, 29, out tokenIsAppContainer, sizeof(int), out returned), "GetTokenInformation(TokenIsAppContainer)");
                isAppContainer = tokenIsAppContainer != 0;
            } finally {
                CloseHandle(token);
            }

            uint waitResult = WaitForSingleObject(processInfo.hProcess, 15000);
            if (waitResult == 0x00000102) {
                TerminateProcess(processInfo.hProcess, 124);
                WaitForSingleObject(processInfo.hProcess, 5000);
                throw new TimeoutException("The AppContainer probe process did not finish within 15 seconds.");
            }
            if (waitResult != 0) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "WaitForSingleObject");
            uint exitCode;
            Check(GetExitCodeProcess(processInfo.hProcess, out exitCode), "GetExitCodeProcess");
            return exitCode;
        } finally {
            if (processInfo.hThread != IntPtr.Zero) CloseHandle(processInfo.hThread);
            if (processInfo.hProcess != IntPtr.Zero) CloseHandle(processInfo.hProcess);
            if (attributes != IntPtr.Zero) {
                DeleteProcThreadAttributeList(attributes);
                Marshal.FreeHGlobal(attributes);
            }
            if (securityCapabilities != IntPtr.Zero) Marshal.FreeHGlobal(securityCapabilities);
            if (sid != IntPtr.Zero) FreeSid(sid);
        }
    }
}
"@

try {
    Add-Type -TypeDefinition $nativeSource -ErrorAction Stop
    $tcpListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $tcpListener.Start()
    $networkPort = ([System.Net.IPEndPoint]$tcpListener.LocalEndpoint).Port

    $probeScript = @"
`$ErrorActionPreference = 'Stop'
`$protectedFile = '$protectedFile'
`$outsideWrite = '$outsideWrite'
`$childWrite = '$childWrite'
`$resultFile = '$resultFile'
`$networkPort = $networkPort
`$readDenied = `$false
try { [System.IO.File]::ReadAllText(`$protectedFile) | Out-Null } catch { `$readDenied = `$true }
`$writeDenied = `$false
try { [System.IO.File]::WriteAllText(`$outsideWrite, 'outside') } catch { `$writeDenied = `$true }
`$networkDenied = `$false
`$client = [System.Net.Sockets.TcpClient]::new()
try {
  `$connection = `$client.BeginConnect('127.0.0.1', `$networkPort, `$null, `$null)
  if (-not `$connection.AsyncWaitHandle.WaitOne(1500)) { `$networkDenied = `$true }
  else { try { `$client.EndConnect(`$connection) } catch { `$networkDenied = `$true } }
} catch { `$networkDenied = `$true } finally { `$client.Dispose() }
`$childStarted = `$false
`$childWriteDenied = `$false
`$childProcessId = 0
`$childError = 'none'
`$childExecutable = Join-Path `$PSHOME 'powershell.exe'
`$childCommand = "try { [IO.File]::WriteAllText('$childWrite','child') } catch {}; Start-Sleep -Seconds 8"
try {
  `$childScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes(`$childCommand))
  `$childStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
  `$childStartInfo.FileName = `$childExecutable
  `$childStartInfo.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' + `$childScript
  `$childStartInfo.UseShellExecute = `$false
  `$childStartInfo.CreateNoWindow = `$true
  `$child = [System.Diagnostics.Process]::Start(`$childStartInfo)
  if (`$null -ne `$child) {
    `$childStarted = `$true
    `$childProcessId = `$child.Id
  }
} catch { `$childStarted = `$false; `$childError = `$_.Exception.GetType().Name + ':' + (`$_.Exception.Message -replace '[\r\n]+', ' ') }
`$report = @(
  "READ_DENIED=`$readDenied",
  "WRITE_DENIED=`$writeDenied",
  "NETWORK_DENIED=`$networkDenied",
  "CHILD_STARTED=`$childStarted",
  "CHILD_PROCESS_ID=`$childProcessId",
  "CHILD_WRITE_DENIED=`$childWriteDenied",
  "CHILD_ERROR=`$childError"
)
[System.IO.File]::WriteAllLines(`$resultFile, `$report)
if (-not (`$readDenied -and `$writeDenied -and `$networkDenied)) { exit 10 }
exit 0
"@
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probeScript))
    $powershellPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $isAppContainer = $false
    $probeExitCode = [AgentDuAppContainerProbe]::Run($profileName, $powershellPath, $encodedCommand, $resultRoot, [ref]$isAppContainer)
    $report = Get-Content -LiteralPath $resultFile -ErrorAction SilentlyContinue
    $networkDenied = $report -contains "NETWORK_DENIED=True"
    $readDenied = $report -contains "READ_DENIED=True"
    $writeDenied = $report -contains "WRITE_DENIED=True"
    $childStarted = $report -contains "CHILD_STARTED=True"
    $childError = ($report | Where-Object { $_.StartsWith("CHILD_ERROR=") } | Select-Object -First 1) -replace "^CHILD_ERROR=", ""
    $childAppContainer = $false
    $childWriteDenied = $false
    $childIdLine = $report | Where-Object { $_.StartsWith("CHILD_PROCESS_ID=") } | Select-Object -First 1
    if ($childStarted -and $childIdLine) {
        $childProcessId = [uint32]($childIdLine -replace "^CHILD_PROCESS_ID=", "")
        $childAppContainer = [AgentDuAppContainerProbe]::IsProcessAppContainer($childProcessId)
        Wait-Process -Id $childProcessId -Timeout 10 -ErrorAction Stop
        $childWriteDenied = -not (Test-Path -LiteralPath $childWrite)
    }

    [pscustomobject]@{
        AppContainerToken = $isAppContainer
        WorkspaceReadDenied = $readDenied
        WorkspaceWriteDenied = $writeDenied
        LoopbackNetworkDenied = $networkDenied
        ChildProcessStarted = $childStarted
        ChildProcessIsAppContainer = $childAppContainer
        ChildProcessWriteDenied = $childWriteDenied
        ChildProcessError = $childError
        ProbeExitCode = $probeExitCode
        Result = if ($isAppContainer -and $readDenied -and $writeDenied -and $networkDenied -and ((-not $childStarted) -or ($childAppContainer -and $childWriteDenied)) -and $probeExitCode -eq 0) { "PASS" } else { "FAIL" }
    } | Format-List
} finally {
    if ($tcpListener) { $tcpListener.Stop() }
    if ("AgentDuAppContainerProbe" -as [type]) {
        [AgentDuAppContainerProbe]::DeleteProfile($profileName) | Out-Null
    }
    Remove-Item -LiteralPath $resolvedProbeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
