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
#ifndef AppName
  #define AppName "个人事务管理"
#endif
#define GuiExe "PersonalManagement.exe"
#ifndef DataDirectory
  #define DataDirectory "{localappdata}\PersonalManagement\data"
#endif

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
Name: "{group}\{#AppName}"; Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{code:SelectedDataDirectory}"""; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\选择数据空间"; Filename: "{app}\{#GuiExe}"; Parameters: "--choose-data"; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\为已有数据创建快捷方式"; Filename: "{app}\{#GuiExe}"; Parameters: "--install-shortcuts --choose-data"; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"
Name: "{group}\使用说明"; Filename: "{app}\详细使用说明.md"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{code:SelectedDataDirectory}"""; WorkingDir: "{app}"; AppUserModelID: "PersonalManagement.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\{#GuiExe}"; Parameters: "--data-dir ""{code:SelectedDataDirectory}"""; Description: "启动个人事务管理"; Flags: nowait postinstall skipifsilent unchecked

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdentity}_is1';

var
  DataPage: TInputDirWizardPage;
  ExistingInstall: Boolean;
  PreviousAppDirectory: String;

function GetFileAttributesW(FileName: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function NormalDirectory(Value: String): String;
begin
  Result := AddBackslash(ExpandFileName(Value));
end;

function LinkedDirectory(Value: String): Boolean;
var
  Current, Parent: String;
  Attributes: LongWord;
begin
  Result := False;
  Current := ExpandFileName(Value);
  repeat
    Attributes := GetFileAttributesW(Current);
    if (Attributes <> $FFFFFFFF) and ((Attributes and $400) <> 0) then
    begin
      Result := True;
      Exit;
    end;
    Parent := ExtractFileDir(RemoveBackslashUnlessRoot(Current));
    if CompareText(Parent, Current) = 0 then Break;
    Current := Parent;
  until Current = '';
end;

function AbsoluteDirectory(Value: String): Boolean;
begin
  Result := ((Length(Value) >= 3) and (Value[2] = ':') and (Value[3] = '\')) or
    ((Length(Value) >= 5) and (Copy(Value, 1, 2) = '\\'));
end;

function SelectedDataDirectory(Param: String): String;
begin
  Result := Trim(DataPage.Values[0]);
  if Result <> '' then Result := RemoveBackslashUnlessRoot(ExpandFileName(Result));
end;

function ShortcutDataDirectory(FileName: String): String;
var
  Shell, Link: Variant;
  Arguments, Value: String;
begin
  Result := '';
  if not FileExists(FileName) then Exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Link := Shell.CreateShortcut(FileName);
    if CompareText(ExpandFileName(Link.TargetPath),
      ExpandFileName(AddBackslash(PreviousAppDirectory) + '{#GuiExe}')) <> 0 then Exit;
    Arguments := Trim(Link.Arguments);
    if Copy(Arguments, 1, 11) <> '--data-dir ' then Exit;
    Value := Trim(Copy(Arguments, 12, Length(Arguments)));
    if (Length(Value) >= 2) and (Value[1] = '"') and
       (Value[Length(Value)] = '"') then
      Value := Copy(Value, 2, Length(Value) - 2)
    else if Pos(' ', Value) > 0 then Exit;
    if (Pos('"', Value) > 0) or not AbsoluteDirectory(Value) then Exit;
    Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
  except
    { An unreadable or unfamiliar launcher never justifies choosing empty data. }
    Result := '';
  end;
end;

procedure InitializeWizard;
var
  Selected, DesktopData, MenuData: String;
begin
  ExistingInstall := RegQueryStringValue(HKCU, UninstallKey,
    'InstallLocation', PreviousAppDirectory);
  Selected := ExpandConstant('{param:DATADIR|}');
  if (Selected = '') and ExistingInstall then
  begin
    { New releases maintain this canonical choice when changing launchers.
      Shortcuts are only a migration source for older installers. }
    if not RegQueryStringValue(HKCU, UninstallKey, 'DataDirectory', Selected) then
      Selected := '';
    if Selected = '' then
    begin
      DesktopData := ShortcutDataDirectory(ExpandConstant('{userdesktop}\{#AppName}.lnk'));
      MenuData := ShortcutDataDirectory(ExpandConstant('{userprograms}\{#AppName}\{#AppName}.lnk'));
      if (DesktopData <> '') and (MenuData <> '') and
         (CompareText(NormalDirectory(DesktopData), NormalDirectory(MenuData)) <> 0) then
        Selected := ''
      else
      begin
        Selected := DesktopData;
        if Selected = '' then Selected := MenuData;
        if Selected = '' then Selected := GetPreviousData('DataDirectory', '');
      end;
    end;
  end;
  if (Selected = '') and not ExistingInstall then
    Selected := ExpandConstant('{#DataDirectory}');
  DataPage := CreateInputDirPage(wpSelectDir, '保留或选择数据空间',
    '更新程序，并继续使用原来的资料',
    '已有用户请选中原数据目录（包含 database.sqlite3 的文件夹）。' + #13#10 +
    '安装器只替换程序，不移动或清空资料。新用户可以保留默认目录。' + #13#10 +
    '如旧版使用自选目录或便携版，请在这里选择该目录，无需重新导入。', False, '');
  DataPage.Add('本次安装后继续使用的数据目录：');
  DataPage.Values[0] := Selected;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDirectory', SelectedDataDirectory(''));
  if not RegWriteStringValue(HKCU, UninstallKey, 'DataDirectory', SelectedDataDirectory('')) then
    RaiseException('无法保存数据目录设置，请检查当前用户的注册表权限。资料未被移动。');
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

function LinkedEntryInTree(Directory: String): String;
var
  Entry: TFindRec;
  Child: String;
begin
  Result := '';
  if FindFirst(AddBackslash(Directory) + '*', Entry) then
  begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then
        begin
          Child := AddBackslash(Directory) + Entry.Name;
          if (Entry.Attributes and $400) <> 0 then
            Result := Child
          else if (Entry.Attributes and $10) <> 0 then
            Result := LinkedEntryInTree(Child);
          if Result <> '' then Break;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function DataDirectoryError: String;
var
  Directory: String;
begin
  Result := '';
  Directory := Trim(DataPage.Values[0]);
  if Directory = '' then
  begin
    Result := '无法确定旧版使用的数据目录。请选择包含 database.sqlite3 的原数据目录；静默安装请提供 /DATADIR=完整路径。安装器不会改用空的默认空间。';
    Exit;
  end;
  if not AbsoluteDirectory(Directory) or (Pos('"', Directory) > 0) or
     (Pos(#13, Directory) > 0) or (Pos(#10, Directory) > 0) then
  begin
    Result := '数据目录必须是完整的文件夹路径，不能包含引号或换行。';
    Exit;
  end;
  Directory := SelectedDataDirectory('');
  if ExistingInstall and not FileExists(AddBackslash(Directory) + 'database.sqlite3') then
  begin
    Result := '升级时必须继续使用已有数据空间，但所选目录中没有 database.sqlite3。请检查数据盘是否已连接，或重新选择原数据目录。安装器不会新建空库来替代原资料。';
    Exit;
  end;
  if LinkedDirectory(Directory) then
  begin
    Result := '数据目录或其上级是链接目录，无法安全核对程序与资料的边界。请选择资料实际存放的目录。';
    Exit;
  end;
  if SameOrInside(Directory, ExpandConstant('{app}')) or
     SameOrInside(ExpandConstant('{app}'), Directory) then
  begin
    Result := '程序目录与数据目录不能相同，也不能互相包含。请选择独立的数据目录，原资料不会被移动或删除。';
    Exit;
  end;
  if FileExists(AddBackslash(Directory) + 'restore_pending.json') then
  begin
    Result := '这个数据空间的恢复尚未完成。请先完成恢复，或选择原来可用的数据空间。';
    Exit;
  end;
  if FileExists(Directory) or (DirExists(Directory) and DirectoryHasEntries(Directory) and
     not FileExists(AddBackslash(Directory) + 'database.sqlite3')) then
    Result := '所选数据目录已有其他文件，但不是已有软件数据空间。请选择包含 database.sqlite3 的原目录，或选择新的空目录。';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then
  begin
    Problem := DataDirectoryError;
    Result := Problem = '';
    if not Result then SuppressibleMsgBox(Problem, mbError, MB_OK, IDOK);
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { PrepareToInstall performs the same checks for silent installs, including
    the useful legacy-data error instead of the directory widget's generic one. }
  Result := WizardSilent and (PageID = DataPage.ID);
end;

function NextVersionNumber(var Version: String): Integer;
var
  Dot: Integer;
  Component: String;
begin
  if Version = '' then
  begin
    Result := 0;
    Exit;
  end;
  Dot := Pos('.', Version);
  if Dot = 0 then
  begin
    Component := Version;
    Version := '';
  end
  else
  begin
    Component := Copy(Version, 1, Dot - 1);
    Version := Copy(Version, Dot + 1, Length(Version));
  end;
  Result := StrToIntDef(Component, -1);
end;

function InstalledVersionError: String;
var
  Previous, Target, DisplayVersion: String;
  Index, PreviousPart, TargetPart, Comparison: Integer;
begin
  Result := '';
  if not ExistingInstall then Exit;
  if not RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', DisplayVersion) or
     (DisplayVersion = '') then
  begin
    Result := '无法核对旧程序的版本，安装尚未开始。请先修复原安装信息，以免把较新程序替换成旧版。';
    Exit;
  end;
  Previous := DisplayVersion;
  Target := '{#AppVersion}';
  Comparison := 0;
  for Index := 1 to 4 do
  begin
    PreviousPart := NextVersionNumber(Previous);
    TargetPart := NextVersionNumber(Target);
    if (PreviousPart < 0) or (TargetPart < 0) then
    begin
      Result := '无法核对旧程序的版本：' + DisplayVersion + '。安装尚未开始。';
      Exit;
    end;
    if Comparison = 0 then
    begin
      if PreviousPart > TargetPart then Comparison := 1;
      if PreviousPart < TargetPart then Comparison := -1;
    end;
  end;
  if Previous <> '' then
    Result := '无法核对旧程序的版本：' + DisplayVersion + '。安装尚未开始。'
  else if Comparison > 0 then
    Result := '已安装的 ' + DisplayVersion + ' 高于本安装包 {#AppVersion}，不能直接降级。原程序和资料均未修改，请使用较新的安装包。';
end;

function InstallationDirectoryError: String;
var
  Directory, Current, Parent, Previous: String;
begin
  Result := '';
  Directory := ExpandConstant('{app}');
  if LinkedDirectory(Directory) then
  begin
    Result := '程序目录或其上级是链接目录，无法安全核对文件归属。请选择实际的程序目录。';
    Exit;
  end;
  if ExistingInstall and
     (CompareText(NormalDirectory(Directory), NormalDirectory(PreviousAppDirectory)) <> 0) then
  begin
    Result := '升级需要替换已登记的旧程序。请使用原程序目录：' + PreviousAppDirectory + '。业务数据目录可在下一页选择。';
    Exit;
  end;
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
  Executable, LinkedEntry: String;
begin
  Result := '';
  if LinkedDirectory(ExpandConstant('{app}')) then
  begin
    Result := '程序目录是链接目录，无法安全核对文件归属，尚未修改安装文件。';
    Exit;
  end;
  LinkedEntry := LinkedEntryInTree(ExpandConstant('{app}'));
  if LinkedEntry <> '' then
  begin
    Result := '程序目录内存在链接文件或链接目录，可能指向个人资料，尚未修改安装文件：' + LinkedEntry;
    Exit;
  end;
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
        Result := '此安装目录中的软件或后台服务仍在运行。请先保存工作，在软件中使用“退出并准备更新”。Codex 已加载的本软件接口也会占用旧文件：等任务结束后暂时关闭 Codex，再运行安装包；仅刷新 MCP 会重新占用文件。旧版没有退出后台入口时，请在下次 Windows 登录后、启动软件前更新。安装器不会强制关闭程序或 Codex。';
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
  Result := InstalledVersionError;
  if Result = '' then Result := InstallationDirectoryError;
  if Result = '' then Result := DataDirectoryError;
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
    '继续使用的数据：' + SelectedDataDirectory('') + NewLine +
    '更新会替换旧程序，并保留这个数据目录中的记录、资料、附件、设置及备份；无需重新导入。' + NewLine +
    '安装包不含个人记录，卸载也不会删除业务数据和备份。' + NewLine +
    '更新前请保存工作，使用“退出并准备更新”；若 Codex 已加载本软件接口，等任务结束后暂时关闭 Codex，再安装。安装器不会强制结束任务。';
end;
