$ErrorActionPreference = "Stop"

$requestText = [Console]::In.ReadToEnd()
$request = ConvertFrom-Json -InputObject $requestText
$workspace = [System.IO.Path]::GetFullPath([string]$request.workspace)
$command = [string]$request.command
$timeoutSeconds = [Math]::Max(1, [Math]::Min(60, [int]$request.timeoutSeconds))
$runId = [string]$request.runId
if ($runId -notmatch '^[a-f0-9]{32}$') { throw "Invalid command run identifier." }
$profileName = "AgentDU.Command.$([guid]::NewGuid().ToString('N'))"
$temporaryRoot = Join-Path $workspace ".agentdu\command-runs"
$temporaryDirectory = Join-Path $temporaryRoot $runId
$changedPaths = [System.Collections.Generic.List[string]]::new()
$protectedDirectories = [System.Collections.Generic.List[object]]::new()
$cleanupErrors = [System.Collections.Generic.List[string]]::new()
$profileCreated = $false
$sid = $null

$nativeSource = @"
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class AgentDuAppContainerCommand {
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
    private struct SECURITY_ATTRIBUTES {
        public int Length;
        public IntPtr SecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION {
        public IntPtr Process;
        public IntPtr Thread;
        public uint ProcessId;
        public uint ThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int CreateAppContainerProfile(string name, string displayName, string description, IntPtr capabilities, uint capabilityCount, out IntPtr appContainerSid);
    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeriveAppContainerSidFromAppContainerName(string name, out IntPtr appContainerSid);
    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeleteAppContainerProfile(string name);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool ConvertSidToStringSid(IntPtr sid, out IntPtr stringSid);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returnSize);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CreateProcessW(string application, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string currentDirectory, ref STARTUPINFOEX startup, out PROCESS_INFORMATION processInfo);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CreatePipe(out IntPtr readPipe, out IntPtr writePipe, ref SECURITY_ATTRIBUTES attributes, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteFile(IntPtr handle, byte[] buffer, uint bytesToWrite, out uint bytesWritten, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadFile(IntPtr handle, byte[] buffer, uint bytesToRead, out uint bytesRead, IntPtr overlapped);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int informationClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION information, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int informationClass, out int information, int length, out int returnLength);

    public static string CreateProfile(string name) {
        IntPtr sid;
        int result = CreateAppContainerProfile(name, name, "AgentDU isolated project command", IntPtr.Zero, 0, out sid);
        if (result == unchecked((int)0x800700B7)) {
            result = DeriveAppContainerSidFromAppContainerName(name, out sid);
            if (result != 0) Marshal.ThrowExceptionForHR(result);
        } else if (result != 0) Marshal.ThrowExceptionForHR(result);
        try {
            IntPtr text;
            Check(ConvertSidToStringSid(sid, out text), "ConvertSidToStringSid");
            try { return Marshal.PtrToStringUni(text); }
            finally { LocalFree(text); }
        } finally { FreeSid(sid); }
    }

    public static int OemCodePage() { return (int)GetOEMCP(); }
    public static int DeleteProfile(string name) { return DeleteAppContainerProfile(name); }

    public static uint Run(string name, string executable, string workingDirectory, string commandScriptPath, int timeoutSeconds, out bool isAppContainer, out string outputBase64) {
        IntPtr sid = IntPtr.Zero;
        IntPtr attributes = IntPtr.Zero;
        IntPtr capabilitiesPointer = IntPtr.Zero;
        IntPtr inheritedHandles = IntPtr.Zero;
        IntPtr stdinRead = IntPtr.Zero, stdinWrite = IntPtr.Zero;
        IntPtr stdoutRead = IntPtr.Zero, stdoutWrite = IntPtr.Zero;
        IntPtr job = IntPtr.Zero;
        PROCESS_INFORMATION processInfo = new PROCESS_INFORMATION();
        MemoryStream captured = new MemoryStream();
        bool timedOut = false;
        try {
            int result = DeriveAppContainerSidFromAppContainerName(name, out sid);
            if (result != 0) Marshal.ThrowExceptionForHR(result);
            SECURITY_CAPABILITIES securityCapabilities = new SECURITY_CAPABILITIES();
            securityCapabilities.AppContainerSid = sid;
            capabilitiesPointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)));
            Marshal.StructureToPtr(securityCapabilities, capabilitiesPointer, false);
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
            if (size == IntPtr.Zero) throw new InvalidOperationException("Windows did not report an attribute list size.");
            attributes = Marshal.AllocHGlobal(size);
            Check(InitializeProcThreadAttributeList(attributes, 2, 0, ref size), "InitializeProcThreadAttributeList");
            Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x00020009), capabilitiesPointer, new IntPtr(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES))), IntPtr.Zero, IntPtr.Zero), "Set AppContainer security capabilities");

            SECURITY_ATTRIBUTES pipeSecurity = new SECURITY_ATTRIBUTES();
            pipeSecurity.Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            pipeSecurity.InheritHandle = true;
            Check(CreatePipe(out stdinRead, out stdinWrite, ref pipeSecurity, 0), "CreatePipe stdin");
            Check(SetHandleInformation(stdinWrite, 1, 0), "Make stdin writer private");
            CloseHandle(stdinWrite); stdinWrite = IntPtr.Zero;
            Check(CreatePipe(out stdoutRead, out stdoutWrite, ref pipeSecurity, 0), "CreatePipe stdout");
            Check(SetHandleInformation(stdoutRead, 1, 0), "Make stdout reader private");
            inheritedHandles = Marshal.AllocHGlobal(IntPtr.Size * 2);
            Marshal.WriteIntPtr(inheritedHandles, 0, stdinRead);
            Marshal.WriteIntPtr(inheritedHandles, IntPtr.Size, stdoutWrite);
            Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x00020002), inheritedHandles, new IntPtr(IntPtr.Size * 2), IntPtr.Zero, IntPtr.Zero), "Limit inherited handles");

            STARTUPINFOEX startup = new STARTUPINFOEX();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            startup.StartupInfo.dwFlags = 0x00000100;
            startup.StartupInfo.hStdInput = stdinRead;
            startup.StartupInfo.hStdOutput = stdoutWrite;
            startup.StartupInfo.hStdError = stdoutWrite;
            startup.lpAttributeList = attributes;
            StringBuilder commandLine = new StringBuilder("\"" + executable + "\" /d /q /s /c \"\"" + commandScriptPath + "\"\"");
            Check(CreateProcessW(executable, commandLine, IntPtr.Zero, IntPtr.Zero, true, 0x00080000 | 0x08000000 | 0x00000004, IntPtr.Zero, workingDirectory, ref startup, out processInfo), "CreateProcessW AppContainer");
            CloseHandle(stdinRead); stdinRead = IntPtr.Zero;
            CloseHandle(stdoutWrite); stdoutWrite = IntPtr.Zero;

            IntPtr token;
            Check(OpenProcessToken(processInfo.Process, 0x0008, out token), "OpenProcessToken");
            try {
                int tokenIsAppContainer, returned;
                Check(GetTokenInformation(token, 29, out tokenIsAppContainer, sizeof(int), out returned), "GetTokenInformation");
                isAppContainer = tokenIsAppContainer != 0;
            } finally { CloseHandle(token); }
            if (!isAppContainer) throw new InvalidOperationException("Command process did not receive an AppContainer token.");

            job = CreateJobObject(IntPtr.Zero, null);
            Check(job != IntPtr.Zero, "CreateJobObject");
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000100 | 0x00000008;
            limits.BasicLimitInformation.ActiveProcessLimit = 64;
            limits.ProcessMemoryLimit = new UIntPtr(1073741824);
            Check(SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))), "SetInformationJobObject");
            Check(AssignProcessToJobObject(job, processInfo.Process), "AssignProcessToJobObject");

            Thread outputReader = new Thread(delegate() {
                byte[] buffer = new byte[8192];
                uint count;
                while (ReadFile(stdoutRead, buffer, (uint)buffer.Length, out count, IntPtr.Zero) && count > 0) {
                    int keep = Math.Min((int)count, Math.Max(0, 128000 - (int)captured.Length));
                    if (keep > 0) captured.Write(buffer, 0, keep);
                }
            });
            outputReader.IsBackground = true;
            outputReader.Start();
            if (ResumeThread(processInfo.Thread) == 0xFFFFFFFF) throw new Win32Exception(Marshal.GetLastWin32Error(), "ResumeThread");

            uint wait = WaitForSingleObject(processInfo.Process, (uint)(timeoutSeconds * 1000));
            if (wait == 0x00000102) {
                timedOut = true;
                TerminateJobObject(job, 124);
                WaitForSingleObject(processInfo.Process, 5000);
            } else if (wait != 0) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "WaitForSingleObject");
            }
            uint exitCode;
            Check(GetExitCodeProcess(processInfo.Process, out exitCode), "GetExitCodeProcess");
            if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
            if (!outputReader.Join(5000)) {
                TerminateProcess(processInfo.Process, 124);
                outputReader.Join(2000);
            }
            outputBase64 = Convert.ToBase64String(captured.ToArray());
            if (timedOut) return 124;
            return exitCode;
        } finally {
            if (job != IntPtr.Zero) { TerminateJobObject(job, 124); CloseHandle(job); }
            if (processInfo.Thread != IntPtr.Zero) CloseHandle(processInfo.Thread);
            if (processInfo.Process != IntPtr.Zero) CloseHandle(processInfo.Process);
            if (stdinRead != IntPtr.Zero) CloseHandle(stdinRead);
            if (stdinWrite != IntPtr.Zero) CloseHandle(stdinWrite);
            if (stdoutRead != IntPtr.Zero) CloseHandle(stdoutRead);
            if (stdoutWrite != IntPtr.Zero) CloseHandle(stdoutWrite);
            if (attributes != IntPtr.Zero) { DeleteProcThreadAttributeList(attributes); Marshal.FreeHGlobal(attributes); }
            if (inheritedHandles != IntPtr.Zero) Marshal.FreeHGlobal(inheritedHandles);
            if (capabilitiesPointer != IntPtr.Zero) Marshal.FreeHGlobal(capabilitiesPointer);
            if (sid != IntPtr.Zero) FreeSid(sid);
            captured.Dispose();
        }
    }

    [DllImport("kernel32.dll")]
    private static extern uint GetOEMCP();
    [DllImport("advapi32.dll")]
    private static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("kernel32.dll")]
    private static extern bool DeleteProcThreadAttributeList(IntPtr list);
    private static void Check(bool success, string operation) {
        if (!success) {
            int error = Marshal.GetLastWin32Error();
            throw new Win32Exception(error, operation + " (Win32 " + error + ")");
        }
    }
}
"@

function Get-WorkspaceEntries([string]$root) {
    $entries = [System.Collections.Generic.List[string]]::new()
    $protected = [System.Collections.Generic.List[string]]::new()
    $stack = [System.Collections.Generic.Stack[string]]::new()
    $entries.Add($root)
    $stack.Push($root)
    while ($stack.Count -gt 0) {
        $current = $stack.Pop()
        foreach ($entry in [System.IO.Directory]::EnumerateFileSystemEntries($current)) {
            $attributes = [System.IO.File]::GetAttributes($entry)
            if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if (($attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
                if ([System.IO.Path]::GetFileName($entry) -ieq ".agentdu") {
                    $protected.Add($entry)
                    continue
                }
                $entries.Add($entry)
                $stack.Push($entry)
            } else {
                $entries.Add($entry)
            }
        }
    }
    return @{ Entries = $entries; Protected = $protected }
}

function Set-DirectoryAccess([string]$itemPath, [System.Security.Principal.SecurityIdentifier]$identity, [bool]$grant) {
    $security = [System.IO.Directory]::GetAccessControl($itemPath)
    if ($grant) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, [System.Security.AccessControl.FileSystemRights]::Modify, [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    } else {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, [System.Security.AccessControl.FileSystemRights]::Modify, [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow)
        $security.RemoveAccessRuleSpecific($rule)
    }
    [System.IO.Directory]::SetAccessControl($itemPath, $security)
}

function Set-FileAccess([string]$itemPath, [System.Security.Principal.SecurityIdentifier]$identity, [bool]$grant) {
    $security = [System.IO.File]::GetAccessControl($itemPath)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, [System.Security.AccessControl.FileSystemRights]::Modify, [System.Security.AccessControl.AccessControlType]::Allow)
    if ($grant) { $security.AddAccessRule($rule) } else { $security.RemoveAccessRuleSpecific($rule) }
    [System.IO.File]::SetAccessControl($itemPath, $security)
}

try {
    Add-Type -TypeDefinition $nativeSource -ErrorAction Stop
    [System.IO.Directory]::CreateDirectory($temporaryDirectory) | Out-Null
    $sidText = [AgentDuAppContainerCommand]::CreateProfile($profileName)
    $profileCreated = $true
    $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
    $layout = Get-WorkspaceEntries $workspace

    foreach ($directory in $layout.Protected) {
        $acl = [System.IO.Directory]::GetAccessControl($directory)
        $wasProtected = $acl.AreAccessRulesProtected
        if (-not $wasProtected) {
            $acl.SetAccessRuleProtection($true, $true)
            [System.IO.Directory]::SetAccessControl($directory, $acl)
            $protectedDirectories.Add(@{ Path = $directory; WasProtected = $false })
        }
    }

    foreach ($entry in $layout.Entries) {
        $attributes = [System.IO.File]::GetAttributes($entry)
        if (($attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
            Set-DirectoryAccess $entry $sid $true
        } else {
            Set-FileAccess $entry $sid $true
        }
        $changedPaths.Add($entry)
    }
    Set-DirectoryAccess $temporaryDirectory $sid $true
    $changedPaths.Add($temporaryDirectory)

    $commandScriptPath = Join-Path $temporaryDirectory "run.cmd"
    $commandScript = $command + "`r`nexit /b %ERRORLEVEL%`r`n"
    $commandEncoding = [System.Text.Encoding]::GetEncoding([AgentDuAppContainerCommand]::OemCodePage())
    [System.IO.File]::WriteAllText($commandScriptPath, $commandScript, $commandEncoding)

    $cmdExecutable = Join-Path $env:WINDIR "System32\cmd.exe"
    $isAppContainer = $false
    $output = ""
    $exitCode = [AgentDuAppContainerCommand]::Run($profileName, $cmdExecutable, $workspace, $commandScriptPath, $timeoutSeconds, [ref]$isAppContainer, [ref]$output)
    [ordered]@{ exitCode = [int]$exitCode; timedOut = ($exitCode -eq 124); isAppContainer = $isAppContainer; outputBase64 = $output } | ConvertTo-Json -Compress
} catch {
    $detail = $_.Exception.Message
    if ($_.Exception.InnerException) { $detail += " | " + $_.Exception.InnerException.Message }
    [Console]::Error.WriteLine($_.Exception.GetType().Name + " at line " + $_.InvocationInfo.ScriptLineNumber + ": " + $detail)
    exit 1
} finally {
    if ($sid -ne $null) {
        for ($index = $changedPaths.Count - 1; $index -ge 0; $index--) {
            $entry = $changedPaths[$index]
            try {
                if ([System.IO.Directory]::Exists($entry)) { Set-DirectoryAccess $entry $sid $false }
                elseif ([System.IO.File]::Exists($entry)) { Set-FileAccess $entry $sid $false }
            } catch { $cleanupErrors.Add("ACL cleanup failed for a workspace entry: " + $_.Exception.GetType().Name) }
        }
        for ($index = $protectedDirectories.Count - 1; $index -ge 0; $index--) {
            $entry = $protectedDirectories[$index]
            try {
                $acl = [System.IO.Directory]::GetAccessControl($entry.Path)
                $acl.SetAccessRuleProtection($false, $true)
                [System.IO.Directory]::SetAccessControl($entry.Path, $acl)
            } catch { $cleanupErrors.Add("ACL inheritance restore failed for a workspace directory: " + $_.Exception.GetType().Name) }
        }
    }
    if ($profileCreated -and [AgentDuAppContainerCommand]::DeleteProfile($profileName) -ne 0) {
        $cleanupErrors.Add("Windows could not remove the temporary AppContainer profile.")
    }
    try { Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction Stop }
    catch { $cleanupErrors.Add("Temporary command files could not be removed: " + $_.Exception.GetType().Name) }
    if ($cleanupErrors.Count -gt 0) {
        foreach ($cleanupError in $cleanupErrors) { [Console]::Error.WriteLine($cleanupError) }
        exit 3
    }
}
