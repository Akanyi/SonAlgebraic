; SonAlgebraic SDK 安装包
;
; 用 installer/build_installer.py 构建——它会先跑 PyInstaller 产出 sonc.exe，
; 再把版本号通过 /D 传进来。直接用 ISCC 编译这个文件会因为缺 SadkVersion 而失败。

#ifndef SadkVersion
  #error 请通过 build_installer.py 构建，或手动传入 /DSadkVersion=x.y.z
#endif

#define SadkName "SonAlgebraic SDK"
#define SadkPublisher "SonAlgebraic"
#define SadkUrl "https://github.com/Akanyi/SonAlgebraic"
#define SourceRoot ".."
#define DistRoot "..\build\sadk-dist\sonc"

; zig 官方发布的 Windows 构建。版本升级时这三行要一起改——hash 对不上安装器会
; 直接报错中止，这正是想要的：宁可装不上，也不能把来路不明的编译器塞进用户机器。
#define ZigVersion "0.16.0"
#define ZigUrlX64 "https://ziglang.org/download/0.16.0/zig-x86_64-windows-0.16.0.zip"
#define ZigHashX64 "68659eb5f1e4eb1437a722f1dd889c5a322c9954607f5edcf337bc3684a75a7e"
#define ZigSizeX64 97217739
#define ZigUrlArm64 "https://ziglang.org/download/0.16.0/zig-aarch64-windows-0.16.0.zip"
#define ZigHashArm64 "aee38316ee4111717900f45dd3130145c39289e105541d737eb8c5ed653c78ef"
#define ZigSizeArm64 93109828

[Setup]
AppId={{1F534355-B65D-43A6-8326-7D888196555B}
AppName={#SadkName}
AppVersion={#SadkVersion}
AppVerName={#SadkName} {#SadkVersion}
AppPublisher={#SadkPublisher}
AppPublisherURL={#SadkUrl}
AppSupportURL={#SadkUrl}
AppUpdatesURL={#SadkUrl}
DefaultDirName={autopf}\SADK
DefaultGroupName={#SadkName}
UninstallDisplayName={#SadkName} {#SadkVersion}
UninstallDisplayIcon={app}\bin\sonc.exe
OutputDir={#SourceRoot}\build\installer
OutputBaseFilename=SADK-Setup-{#SadkVersion}
SetupIconFile=assets\sadk.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; zig 是 .zip，enhanced 只认 .7z
ArchiveExtraction=full
; 默认装到用户目录，不弹 UAC；想装到 Program Files 的人可以在向导第一页选"所有用户"
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 改了 PATH 就得广播 WM_SETTINGCHANGE，否则已开着的资源管理器和终端拿不到新值
ChangesEnvironment=yes
ChangesAssociations=yes
ArchitecturesAllowed=x64compatible or arm64
ShowLanguageDialog=no
DisableWelcomePage=no

[Languages]
; 简体中文不在 Inno 官方发行包里，用仓库自带的那份，见 languages/README.md
Name: "chs"; MessagesFile: "languages\ChineseSimplified.isl"

[Types]
Name: "full"; Description: "完整安装"
Name: "compact"; Description: "仅编译器"
Name: "custom"; Description: "自定义"; Flags: iscustom

[Components]
Name: "compiler"; Description: "sonc 编译器（必需）"; Types: full compact custom; Flags: fixed
Name: "docs"; Description: "文档"; Types: full
Name: "examples"; Description: "示例程序"; Types: full
Name: "vscode"; Description: "VSCode 语法高亮扩展"; Types: full

[Tasks]
Name: "addtopath"; Description: "把 sonc 加入 PATH 环境变量"; GroupDescription: "系统集成:"
Name: "assoc"; Description: "关联 .sa 文件并添加右键菜单"; GroupDescription: "系统集成:"
Name: "zig"; Description: "下载并安装 Zig C 工具链（约 {#(ZigSizeX64 + 1048575) / 1048576} MB，sonc build / run 必需）"; GroupDescription: "C 工具链:"; Flags: unchecked
Name: "vscodeext"; Description: "安装到 VSCode 用户扩展目录"; GroupDescription: "编辑器:"; Components: vscode; Check: VSCodeExtensionsDirExists

[Files]
Source: "{#DistRoot}\sonc.exe"; DestDir: "{app}\bin"; Components: compiler; Flags: ignoreversion
Source: "{#DistRoot}\_internal\*"; DestDir: "{app}\bin\_internal"; Components: compiler; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "sadk-env.cmd"; DestDir: "{app}\bin"; Components: compiler; Flags: ignoreversion
Source: "{#SourceRoot}\README.md"; DestDir: "{app}"; Components: compiler; Flags: ignoreversion
Source: "{#SourceRoot}\docs\*"; DestDir: "{app}\docs"; Components: docs; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\examples\*.sa"; DestDir: "{app}\examples"; Components: examples; Flags: ignoreversion
Source: "{#SourceRoot}\editors\vscode\sonalgebraic\*"; DestDir: "{app}\editors\vscode\sonalgebraic"; Components: vscode; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\editors\vscode\sonalgebraic\*"; DestDir: "{code:VSCodeExtensionDir}"; Components: vscode; Tasks: vscodeext; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall

; zig 工具链：直接用 [Files] 的 download 标志，而不是在 [Code] 里挂下载页。
; NextButtonClick 只在向导模式下触发，/VERYSILENT 安装时压根不会调用——那样勾了任务
; 也下载不到东西，再被 skipifsourcedoesntexist 静默跳过，用户会以为工具链装上了。
; download 标志走的是安装器内建流程，两种模式行为一致，还自带进度和 SHA-256 校验。
; 解压出来的目录名带版本号（zig-x86_64-windows-0.16.0），不做重命名——sdk_env.py 的
; 工具链探测本来就认这种目录名，而重命名会让卸载日志里的路径对不上，留下删不掉的残留。
Source: "{#ZigUrlX64}"; DestName: "zig-toolchain.zip"; DestDir: "{app}\toolchain"; \
  Hash: "{#ZigHashX64}"; ExternalSize: {#ZigSizeX64}; Tasks: zig; Check: not IsArm64; \
  Flags: external download extractarchive recursesubdirs createallsubdirs ignoreversion
Source: "{#ZigUrlArm64}"; DestName: "zig-toolchain.zip"; DestDir: "{app}\toolchain"; \
  Hash: "{#ZigHashArm64}"; ExternalSize: {#ZigSizeArm64}; Tasks: zig; Check: IsArm64; \
  Flags: external download extractarchive recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\SADK 命令提示符"; Filename: "{cmd}"; Parameters: "/k ""{app}\bin\sadk-env.cmd"""; WorkingDir: "{userdocs}"; IconFilename: "{app}\bin\sonc.exe"
Name: "{group}\文档"; Filename: "{app}\docs"; Components: docs
Name: "{group}\示例程序"; Filename: "{app}\examples"; Components: examples
Name: "{group}\卸载 {#SadkName}"; Filename: "{uninstallexe}"

[Registry]
; PATH 分 HKLM / HKCU 两条写，因为 Root 不能由代码决定，只能靠 Check 二选一
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; \
  Tasks: addtopath; Check: NeedsAddPathAllUsers(ExpandConstant('{app}\bin'))
Root: HKCU; Subkey: "Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; \
  Tasks: addtopath; Check: NeedsAddPathCurrentUser(ExpandConstant('{app}\bin'))

; HKA 会根据"所有用户/仅本人"自动落到 HKLM 或 HKCU
Root: HKA; Subkey: "Software\Classes\.sa"; ValueType: string; ValueName: ""; \
  ValueData: "SonAlgebraic.Source"; Tasks: assoc; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source"; ValueType: string; ValueName: ""; \
  ValueData: "SonAlgebraic 源文件"; Tasks: assoc; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source\DefaultIcon"; ValueType: string; ValueName: ""; \
  ValueData: "{app}\bin\sonc.exe,0"; Tasks: assoc
; 双击不设默认动作：.sa 是源码，跑起来还是打开编辑器该由用户自己定，
; 装个编译器就抢走双击行为太越界了。只挂右键菜单。
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source\shell\sonccheck"; ValueType: string; ValueName: ""; \
  ValueData: "用 sonc 检查(&C)"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source\shell\sonccheck\command"; ValueType: string; ValueName: ""; \
  ValueData: """{cmd}"" /c """"{app}\bin\sonc.exe"" check ""%1"" & pause"""; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source\shell\soncrun"; ValueType: string; ValueName: ""; \
  ValueData: "用 sonc 运行(&R)"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SonAlgebraic.Source\shell\soncrun\command"; ValueType: string; ValueName: ""; \
  ValueData: """{cmd}"" /c """"{app}\bin\sonc.exe"" run ""%1"" & pause"""; Tasks: assoc

[UninstallDelete]
; zig 解压出来的树很深，卸载日志逐个删完常留一堆空目录
Type: filesandordirs; Name: "{app}\toolchain"

[Code]
var
  TaskDefaultsApplied: Boolean;

function VSCodeExtensionsDir: String;
begin
  Result := AddBackslash(GetEnv('USERPROFILE')) + '.vscode\extensions';
end;

function VSCodeExtensionsDirExists: Boolean;
begin
  Result := DirExists(VSCodeExtensionsDir);
end;

function VSCodeExtensionDir(Param: String): String;
begin
  Result := AddBackslash(VSCodeExtensionsDir) + 'sonalgebraic-{#SadkVersion}';
end;

{ 本机已经有能用的 C 编译器时就别劝人再下 93MB。FileSearch 直接扫 PATH，
  比 Exec 一个 where.exe 轻，也不会闪黑框。 }
function HostCompilerFound: Boolean;
begin
  Result := (FileSearch('zig.exe', GetEnv('PATH')) <> '') or
            (FileSearch('gcc.exe', GetEnv('PATH')) <> '') or
            (FileSearch('clang.exe', GetEnv('PATH')) <> '') or
            (FileSearch('cl.exe', GetEnv('PATH')) <> '');
end;

function PathContains(const RootKey: Integer; const SubKey, Dir: String): Boolean;
var
  Existing: String;
begin
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Existing) then begin
    Result := False;
    exit;
  end;
  { 两头补分号再比，避免 C:\SADK\bin 被 C:\SADK\bin2 误判成已存在 }
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Existing) + ';') > 0;
end;

function NeedsAddPathAllUsers(Dir: String): Boolean;
begin
  Result := IsAdminInstallMode and
            not PathContains(HKEY_LOCAL_MACHINE, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', Dir);
end;

function NeedsAddPathCurrentUser(Dir: String): Boolean;
begin
  Result := (not IsAdminInstallMode) and not PathContains(HKEY_CURRENT_USER, 'Environment', Dir);
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Selected: String;
begin
  // 只在第一次进任务页时替用户做决定，之后他自己勾成什么样就是什么样
  if (CurPageID = wpSelectTasks) and not TaskDefaultsApplied then begin
    TaskDefaultsApplied := True;
    if not HostCompilerFound then begin
      // WizardSelectTasks 会取消掉没列出来的任务，所以得把当前已选的一起带上
      Selected := WizardSelectedTasks(False);
      if Selected <> '' then
        Selected := Selected + ',';
      WizardSelectTasks(Selected + 'zig');
    end;
  end;
end;

// 卸载时把自己加的那段从 PATH 摘掉。Inno 能回滚它写过的注册表值，但这里的 PATH 是
// "旧值;新目录" 这种拼接写入，整体回滚会把用户在这期间加的其它路径一起抹掉，
// 所以只能自己做精确的字符串摘除。
procedure RemoveFromPath(const RootKey: Integer; const SubKey, Dir: String);
var
  Existing, Updated: String;
  Position: Integer;
begin
  if not RegQueryStringValue(RootKey, SubKey, 'Path', Existing) then
    exit;

  Updated := ';' + Existing + ';';
  Position := Pos(';' + Lowercase(Dir) + ';', Lowercase(Updated));
  if Position = 0 then
    exit;

  Delete(Updated, Position, Length(Dir) + 1);
  { 去掉前面补的分号，再收掉可能留下的尾随分号 }
  Delete(Updated, 1, 1);
  if (Length(Updated) > 0) and (Updated[Length(Updated)] = ';') then
    Delete(Updated, Length(Updated), 1);

  RegWriteExpandStringValue(RootKey, SubKey, 'Path', Updated);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  BinDir: String;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  BinDir := ExpandConstant('{app}\bin');
  RemoveFromPath(HKEY_LOCAL_MACHINE, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', BinDir);
  RemoveFromPath(HKEY_CURRENT_USER, 'Environment', BinDir);
end;
