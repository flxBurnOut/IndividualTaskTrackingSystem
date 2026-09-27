; Build with packaging/build_installer.py and the pinned Inno Setup compiler.
; Never install business data or delete an arbitrary application directory.
#ifndef AppVersion
  #error AppVersion must be supplied by build_installer.py
#endif
#ifndef PackageDir
  #error PackageDir must point to the verified onedir release
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build_installer.py
#endif
#ifndef AppIdentity
  #define AppIdentity "{EA7A3A48-A445-44C0-A56D-3F5275DD323E}"
#endif
#define AppName "个人事务管理"
#define GuiExe "PersonalManagement.exe"
#define DataDirectory "{localappdata}\PersonalManagement\data"

[Setup]
AppId={{#AppIdentity}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PersonalManagement
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription=个人事务管理安装程序
DefaultDirName={localappdata}\Programs\PersonalManagement
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupArchitecture=x64
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=PersonalManagement-{#AppVersion}-Setup-x64
SetupIconFile={#PackageDir}\_internal\management\assets\app-icon.ico
UninstallDisplayIcon={app}\{#GuiExe}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
AlwaysRestart=no
SetupMutex=PersonalManagement.Installer
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{#DataDirectory}"""; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\选择数据空间"; Filename: "{app}\{#GuiExe}"; Parameters: "--choose-data"; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\为已有数据创建快捷方式"; Filename: "{app}\{#GuiExe}"; Parameters: "--install-shortcuts --choose-data"; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\使用说明"; Filename: "{app}\详细使用说明.md"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{#DataDirectory}"""; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{#DataDirectory}"""; Description: "启动个人事务管理"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function NormalDirectory(Value: String): String;
begin
  Result := AddBackslash(ExpandFileName(Value));
end;

function SameOrInside(Child, Parent: String): Boolean;
var
  Prefix: String;
begin
  Prefix := NormalDirectory(Parent);
  Result := CompareText(Copy(NormalDirectory(Child), 1, Length(Prefix)), Prefix) = 0;
end;

function DirectoryHasEntries(Directory: String): Boolean;
var
  Entry: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Directory) + '*', Entry) then
  begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then
        begin
          Result := True;
          Break;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function InstallationDirectoryError: String;
var
  Directory, Current, Parent, Previous: String;
begin
  Result := '';
  Directory := ExpandConstant('{app}');
  if SameOrInside(Directory, ExpandConstant('{#DataDirectory}')) or
     SameOrInside(ExpandConstant('{#DataDirectory}'), Directory) then
  begin
    Result := '安装目录必须与业务数据目录分开。请保留默认程序目录，或选择新的空目录。';
    Exit;
  end;
  Current := Directory;
  repeat
    if FileExists(AddBackslash(Current) + 'database.sqlite3') or
       FileExists(AddBackslash(Current) + 'runtime.json') or
       FileExists(AddBackslash(Current) + 'restore_pending.json') then
    begin
      Result := '此目录属于业务数据空间，不能安装程序。数据已保留，请选择其他程序目录。';
      Exit;
    end;
    Parent := ExtractFileDir(RemoveBackslashUnlessRoot(Current));
    if CompareText(Parent, Current) = 0 then Break;
    Current := Parent;
  until Current = '';
  if DirExists(Directory) and DirectoryHasEntries(Directory) then
  begin
    if not RegQueryStringValue(HKCU,
        'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdentity}_is1',
        'InstallLocation', Previous) or
       (CompareText(NormalDirectory(Directory), NormalDirectory(Previous)) <> 0) then
      Result := '所选目录已有其他文件，且不是此安装器登记的程序目录。请选择新的空目录；旧软件与数据不会被覆盖。';
  end;
end;

function RunningApplicationError: String;
var
  Locator, Service, Processes, Process: Variant;
  Index: Integer;
  Executable: String;
begin
  Result := '';
  if not FileExists(ExpandConstant('{app}\PersonalManagement.exe')) and
     not FileExists(ExpandConstant('{app}\PersonalManagementService.exe')) then Exit;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Service.ExecQuery('SELECT ExecutablePath FROM Win32_Process WHERE ' +
      'Name="PersonalManagement.exe" OR Name="PersonalManagementService.exe" OR Name="PersonalManagementCodex.exe"');
    for Index := 0 to Processes.Count - 1 do
    begin
      Process := Processes.ItemIndex(Index);
      if VarIsNull(Process.ExecutablePath) or VarIsEmpty(Process.ExecutablePath) then
      begin
        Result := '无法核对正在运行的软件进程。请保存工作后退出本软件及其后台服务，再重试；安装器不会结束任何进程。';
        Exit;
      end;
      Executable := Process.ExecutablePath;
      if SameOrInside(ExtractFileDir(Executable), ExpandConstant('{app}')) then
      begin
        Result := '此安装目录中的软件或后台服务仍在运行。请先保存工作并正常退出；若后台服务仍占用，可在下次 Windows 登录后、启动本软件前再安装或卸载。安装器不会强制关闭程序，也不会关闭 Codex。';
        Exit;
      end;
    end;
  except
    Result := '无法完成运行状态检查，尚未修改安装文件。请稍后重试；安装器不会跳过检查或强制关闭程序。';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  Result := InstallationDirectoryError;
  if Result = '' then Result := RunningApplicationError;
end;

function InitializeUninstall: Boolean;
var
  Problem: String;
begin
  Problem := RunningApplicationError;
  Result := Problem = '';
  if not Result then SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + MemoGroupInfo + NewLine +
    MemoTasksInfo + NewLine + NewLine +
    '默认业务数据：' + ExpandConstant('{#DataDirectory}') + NewLine +
    '安装包不含个人记录。卸载只移除本次安装的程序文件，保留业务数据和备份。' + NewLine +
    '临时打开其他数据：开始菜单中的“选择数据空间”。' + NewLine +
    '固定使用已有数据：开始菜单中的“为已有数据创建快捷方式”。';
end;
