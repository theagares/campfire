# main/system-proxy.ps1
# Windows 사용자 프록시 설정을 읽고 쓴다. system-proxy.js 가 부른다.
#
# 레지스트리 값(ProxyServer·AutoConfigURL)을 직접 쓰지 않는 이유: 실제 설정은
# Internet Settings\Connections\DefaultConnectionSettings 이진 값에 있고, 문자열
# 값들은 거울일 뿐이다. 거울만 바꾸면 WinHTTP(=Chrome 이 쓰는 API)가 무시할 수
# 있다. Windows 설정 화면과 같은 API(InternetSetOption 연결별 옵션)를 쓴다 —
# 두 표현을 함께 고치고 변경 알림까지 보낸다.
#
#   query                      → 현재 설정 JSON
#   set-pac <url>              → 자동 구성 스크립트(PAC)만 켠다
#   restore <json>             → query 로 받아 둔 값을 그대로 되돌린다
#
# 관리자 권한은 필요 없다(사용자 설정만 만진다).

param(
    [Parameter(Mandatory = $true)][string]$Command,
    [string]$Arg = ""
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class CampfireProxy {
    const int INTERNET_OPTION_REFRESH = 37;
    const int INTERNET_OPTION_SETTINGS_CHANGED = 39;
    const int INTERNET_OPTION_PER_CONNECTION_OPTION = 75;

    public const int FLAGS = 1, PROXY_SERVER = 2, PROXY_BYPASS = 3, AUTOCONFIG_URL = 4, FLAGS_UI = 10;

    // x64 배치: dwOption(4) + 패딩(4) + 공용체(8) = 16. 공용체는 포인터나 DWORD.
    [StructLayout(LayoutKind.Sequential)]
    struct Option { public int dwOption; public IntPtr value; }

    [StructLayout(LayoutKind.Sequential)]
    struct OptionList {
        public int dwSize; public IntPtr pszConnection;
        public int dwOptionCount; public int dwOptionError; public IntPtr pOptions;
    }

    [DllImport("wininet.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool InternetQueryOptionW(IntPtr h, int opt, ref OptionList buf, ref int len);
    [DllImport("wininet.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool InternetSetOptionW(IntPtr h, int opt, ref OptionList buf, int len);
    [DllImport("wininet.dll", SetLastError = true)]
    static extern bool InternetSetOptionW(IntPtr h, int opt, IntPtr buf, int len);
    [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr p);

    static int OptSize { get { return Marshal.SizeOf(typeof(Option)); } }

    // 반환: [flags, server, bypass, pacUrl]. 연결은 LAN(null).
    public static object[] Query(int flagsOption) {
        int n = 4;
        IntPtr buf = Marshal.AllocHGlobal(OptSize * n);
        try {
            int[] ids = { flagsOption, PROXY_SERVER, PROXY_BYPASS, AUTOCONFIG_URL };
            for (int i = 0; i < n; i++) {
                Option o = new Option(); o.dwOption = ids[i];
                Marshal.StructureToPtr(o, buf + i * OptSize, false);
            }
            OptionList list = new OptionList();
            list.dwSize = Marshal.SizeOf(typeof(OptionList));
            list.dwOptionCount = n; list.pOptions = buf;
            int len = list.dwSize;
            if (!InternetQueryOptionW(IntPtr.Zero, INTERNET_OPTION_PER_CONNECTION_OPTION, ref list, ref len))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());

            object[] outv = new object[n];
            for (int i = 0; i < n; i++) {
                Option o = (Option)Marshal.PtrToStructure(buf + i * OptSize, typeof(Option));
                if (i == 0) { outv[i] = (int)o.value.ToInt64(); continue; }
                outv[i] = o.value == IntPtr.Zero ? null : Marshal.PtrToStringUni(o.value);
                if (o.value != IntPtr.Zero) GlobalFree(o.value);
            }
            return outv;
        } finally { Marshal.FreeHGlobal(buf); }
    }

    public static void Set(int flags, string server, string bypass, string pacUrl) {
        int n = 4;
        IntPtr buf = Marshal.AllocHGlobal(OptSize * n);
        IntPtr[] strs = new IntPtr[3];
        try {
            strs[0] = server == null ? IntPtr.Zero : Marshal.StringToHGlobalUni(server);
            strs[1] = bypass == null ? IntPtr.Zero : Marshal.StringToHGlobalUni(bypass);
            strs[2] = pacUrl == null ? IntPtr.Zero : Marshal.StringToHGlobalUni(pacUrl);
            Option[] opts = {
                new Option { dwOption = FLAGS, value = new IntPtr(flags) },
                new Option { dwOption = PROXY_SERVER, value = strs[0] },
                new Option { dwOption = PROXY_BYPASS, value = strs[1] },
                new Option { dwOption = AUTOCONFIG_URL, value = strs[2] },
            };
            for (int i = 0; i < n; i++) Marshal.StructureToPtr(opts[i], buf + i * OptSize, false);
            OptionList list = new OptionList();
            list.dwSize = Marshal.SizeOf(typeof(OptionList));
            list.dwOptionCount = n; list.pOptions = buf;
            if (!InternetSetOptionW(IntPtr.Zero, INTERNET_OPTION_PER_CONNECTION_OPTION, ref list, list.dwSize))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            // 바꿨다고 알린다. 이게 없으면 이미 떠 있는 프로그램이 옛 설정을 계속 쓴다.
            InternetSetOptionW(IntPtr.Zero, INTERNET_OPTION_SETTINGS_CHANGED, IntPtr.Zero, 0);
            InternetSetOptionW(IntPtr.Zero, INTERNET_OPTION_REFRESH, IntPtr.Zero, 0);
        } finally {
            foreach (IntPtr p in strs) if (p != IntPtr.Zero) Marshal.FreeHGlobal(p);
            Marshal.FreeHGlobal(buf);
        }
    }
}
"@

function Get-State {
    # FLAGS_UI(10)가 설정 화면이 보여 주는 진짜 플래그(자동 검색 포함)다. 구형 Windows
    # 에서 안 되면 FLAGS(1)로 물러선다.
    try { $v = [CampfireProxy]::Query([CampfireProxy]::FLAGS_UI) }
    catch { $v = [CampfireProxy]::Query([CampfireProxy]::FLAGS) }
    [ordered]@{ flags = [int]$v[0]; server = $v[1]; bypass = $v[2]; pacUrl = $v[3] }
}

switch ($Command) {
    "query" {
        Get-State | ConvertTo-Json -Compress
    }
    "set-pac" {
        if (-not $Arg) { throw "PAC URL 이 필요하다" }
        # PROXY_TYPE_DIRECT(1) | PROXY_TYPE_AUTO_PROXY_URL(4). 수동 프록시·자동 검색은 끈다 —
        # 켜 두면 우리 PAC 와 섞여 어느 쪽이 적용될지 알 수 없다. 이전 값은 호출자가
        # query 로 받아 두었다가 restore 로 되돌린다.
        $s = Get-State
        [CampfireProxy]::Set(5, $s.server, $s.bypass, $Arg)
        Get-State | ConvertTo-Json -Compress
    }
    "restore" {
        $p = $Arg | ConvertFrom-Json
        [CampfireProxy]::Set([int]$p.flags, $p.server, $p.bypass, $p.pacUrl)
        Get-State | ConvertTo-Json -Compress
    }
    default { throw "모르는 명령: $Command" }
}
