"""Publish only the verified installer and checksum to the authorized repository.

Uses the existing repository credential in memory. Never prints/persists it.
Create/upload as a draft, verify GitHub digests, then publish explicitly.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import re
from workflow import ROOT, version_directory, write_json
from build_installer import source_version, pe_version, validate_package

REPO = TAG = DIRECTORY = STATE = EXE = SUMS = NOTES = TITLE = None
GIT = ['git', '-c', 'safe.directory=' + ROOT.as_posix()]


def configure(version, repository=None):
    global REPO, TAG, DIRECTORY, STATE, EXE, SUMS, NOTES, TITLE
    remote = git('remote', 'get-url', 'origin')
    match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+?)(?:\.git)?', remote)
    if not match:
        raise ValueError('Expected a GitHub HTTPS origin')
    REPO = match[1]
    if repository is not None and repository != REPO:
        raise ValueError('Requested repository differs from origin')
    TAG = 'v' + version
    DIRECTORY = ROOT / '.build' / 'reports' / 'publish' / TAG
    STATE = DIRECTORY / 'publish-state.json'
    folder = version_directory(version, ROOT)
    EXE = folder / ('PersonalManagement-' + version + '-Setup-x64.exe')
    SUMS = folder / 'SHA256SUMS.txt'
    NOTES = ROOT / 'docs' / ('发布说明_' + version + '.md')
    TITLE = '个人事务管理 ' + TAG + ' · ' + NOTES.read_text('utf-8').splitlines()[0].removeprefix('# ' + TAG + ' ')



def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def git(*args):
    return subprocess.check_output([*GIT,*args],cwd=ROOT).decode('utf-8').strip()


def credentials():
    env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never'}
    result=subprocess.run([*GIT,'credential','fill'],input='protocol=https\nhost=github.com\npath='+REPO+'.git\n\n',
                          capture_output=True,text=True,env=env,cwd=ROOT)
    if result.returncode:raise RuntimeError('Repository credential unavailable; no credential was printed.')
    record=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if not record.get('password'):raise RuntimeError('No GitHub repository credential returned.')
    env['GH_TOKEN']=record['password']
    return env


def gh(env,*args):
    value=subprocess.run(['gh',*args],capture_output=True,text=True,encoding='utf-8',env=env,cwd=ROOT,timeout=240)
    if value.returncode:
        message=value.stderr.replace(env['GH_TOKEN'],'[redacted]')[:1200]
        raise RuntimeError('GitHub command failed: '+message)
    return value.stdout


def api(env,path):
    return json.loads(gh(env,'api','repos/'+REPO+'/'+path))


def release(env):
    values=[x for x in api(env,'releases?per_page=100') if x['tag_name']==TAG]
    if len(values)>1:raise RuntimeError('Ambiguous pre-existing release; nothing overwritten.')
    return values[0] if values else None


def save(value):
    write_json(STATE, value)


def verify_assets(value,expected):
    actual={item['name']:item for item in value['assets']}
    if set(actual)!=set(expected):raise RuntimeError('Release assets differ from the reviewed two-file set.')
    for name,entry in expected.items():
        item=actual[name]
        if item['size']!=entry['bytes'] or item.get('digest')!='sha256:'+entry['sha256'] or item['state']!='uploaded':
            raise RuntimeError('Uploaded asset size/digest/state mismatch: '+name)
    return {name:{'bytes':item['size'],'digest':item['digest'],'url':item['browser_download_url']}
            for name,item in actual.items()}


def target_commit(env):
    value=api(env,'git/ref/tags/'+TAG)['object']
    for _ in range(4):
        if value['type']=='commit':return value['sha']
        if value['type']!='tag':break
        value=api(env,'git/tags/'+value['sha'])['object']
    raise RuntimeError('Release tag does not identify a commit.')


def main():
    from management.data_space import require_packaged_channel
    require_packaged_channel()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect','prepare','publish','verify'])
    parser.add_argument('--version', default=source_version())
    parser.add_argument('--repo')
    parser.add_argument('--branch', default='Windows')
    args=parser.parse_args()
    if args.version != source_version():
        parser.error('Release version must match current source')
    configure(args.version,args.repo)
    manifest=json.loads((EXE.parent/'manifest.json').read_text('utf-8'))
    assert manifest['version']==args.version and not manifest['data_included']
    assert pe_version(EXE)==args.version+'.0'
    assert validate_package(EXE.parent/'app',args.version)==manifest['files']
    expected={p.name:{'sha256':digest(p),'bytes':p.stat().st_size} for p in (EXE,SUMS)}
    assert expected[EXE.name]['sha256']==manifest['installer_sha256']
    assert expected[EXE.name]['bytes']==manifest['installer_bytes']
    assert SUMS.read_text('ascii')==manifest['installer_sha256']+'  '+EXE.name+'\n'
    env=credentials()
    value=release(env)
    if args.mode=='inspect':
        print(json.dumps({'repository':REPO,'tag':TAG,'assets':expected,'release_exists':value is not None,'draft':value.get('draft') if value else None},ensure_ascii=False))
        return
    assert git('branch','--show-current')==args.branch and not git('status','--porcelain')
    head=git('rev-parse','HEAD')
    assert git('ls-remote','--heads','origin','refs/heads/'+args.branch).split()[0]==head
    if git('ls-remote','--tags','origin','refs/tags/'+TAG):
        assert target_commit(env)==head, 'Existing tag points to a different commit'
    if STATE.exists():
        state=json.loads(STATE.read_text('utf-8'))
        assert state['repository']==REPO and state['tag']==TAG and state['commit']==head and state['assets_expected']==expected
    else:
        if value is not None:raise RuntimeError('Release already exists without this publication receipt; refusing to overwrite.')
        state={'repository':REPO,'tag':TAG,'commit':head,'assets_expected':expected,'stage':'prepared','started_at':time.time()}
        save(state)
    if args.mode=='prepare':
        if value is None:
            print('Creating draft and uploading verified installer/checksum...',flush=True)
            gh(env,'release','create',TAG,str(EXE),str(SUMS),'--repo',REPO,'--target',head,'--title',TITLE,
               '--notes-file',str(NOTES),'--draft')
            value=release(env)
        elif not value['draft']:
            raise RuntimeError('Release is already published; use verification mode.')
        else:
            assert value['target_commitish']==head
            existing={x['name']:x for x in value['assets']}
            assert set(existing)<=set(expected)
            for path in (EXE,SUMS):
                if path.name not in existing:gh(env,'release','upload',TAG,str(path),'--repo',REPO)
            value=release(env)
        assert value and value['draft'] and value['target_commitish']==head and value['body']==NOTES.read_text('utf-8')
        verified=verify_assets(value,expected)
        state.update(stage='draft_assets_verified',release_id=value['id'],assets_verified=verified)
        save(state)
        print(json.dumps({'stage':state['stage'],'release_id':value['id'],'assets':verified},ensure_ascii=False),flush=True)
        return
    assert value and value['id']==state['release_id']
    verify_assets(value,expected)
    assert value['body']==NOTES.read_text('utf-8') and value['target_commitish']==head
    if args.mode=='publish' and value['draft']:
        assert state['stage']=='draft_assets_verified'
        print('Verified upload complete; publishing '+TAG+' as latest...',flush=True)
        gh(env,'release','edit',TAG,'--repo',REPO,'--draft=false','--latest','--target',head)
        value=release(env)
    assert value and not value['draft'] and not value['prerelease']
    verified=verify_assets(value,expected)
    assert target_commit(env)==head
    assert api(env,'releases/latest')['tag_name']==TAG
    state.update(stage='published_verified',url=value['html_url'],assets_verified=verified,
                 published_at=value['published_at'],latest=True,tag_commit_verified=True,verified_at=time.time())
    save(state)
    print(json.dumps({'stage':state['stage'],'url':state['url'],'commit':head,'latest':True,'assets':verified},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
