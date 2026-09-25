from pathlib import Path
from PyInstaller.utils.win32.versioninfo import VSVersionInfo,FixedFileInfo,StringFileInfo,StringTable,StringStruct,VarFileInfo,VarStruct
import re
ROOT=Path(__file__).resolve().parents[1]

def write_versions():
    (ROOT/'.build').mkdir(exist_ok=True)
    source=(ROOT/'src/management/__init__.py').read_text('utf-8')
    version=re.search(r'__version__\s*=\s*[\"\x27]([0-9.]+)',source).group(1)
    numbers=tuple(int(x) for x in version.split('.'));numbers=(*numbers,*([0]*(4-len(numbers))))
    for key,filename,description in [('gui','PersonalManagement.exe','个人事务管理'),('service','PersonalManagementService.exe','个人事务管理后台服务'),('shim','PersonalManagementCodex.exe','个人事务管理 Codex 桌面连接')]:
        info=VSVersionInfo(ffi=FixedFileInfo(filevers=numbers,prodvers=numbers,mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[
            StringFileInfo([StringTable('080404B0',[StringStruct(k,v) for k,v in {'CompanyName':'PersonalManagement','FileDescription':description,'FileVersion':version,'InternalName':Path(filename).stem,'OriginalFilename':filename,'ProductName':'个人事务管理','ProductVersion':version}.items()])]),
            VarFileInfo([VarStruct('Translation',[0x0804,1200])])])
        (ROOT/'.build'/('version-'+key+'.txt')).write_text(str(info),'utf-8')

if __name__=='__main__':write_versions()
