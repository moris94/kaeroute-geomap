#!/usr/bin/env python3
"""Reproducible Geofabrik -> Planetiler -> grid PMTiles release pipeline.

Only generated public geographic data is uploaded. Never include routes, user
locations, application sources, API keys, or local config in release assets.
"""
import argparse
import hashlib
import http.client
import contextlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parent
ATTRIBUTION = '© OpenStreetMap contributors · https://www.openstreetmap.org/copyright'
SOURCES = {'JP': 'japan', 'TW': 'taiwan'}
# Dated inputs, not `latest`: reruns use identical geographic snapshots.
SOURCE_DATE = '260901'
PLANETILER_VERSION = '0.10.2'
PLANETILER_SHA256 = 'f310bd0413e2e4512b27f4046d418664e8e1d3bf31603c2a70e23de06c167e4d'


def run(*args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def grid(country, size=15):
    return {'latitudeStep': size * 1000 / 111320,
            'longitudeStep': size * 1000 / (111320 * math.cos(math.radians(36 if country == 'JP' else 24))),
            'bufferMeters': 1000}


def cell(x, y, g):
    return [-180 + x*g['longitudeStep'], -90 + y*g['latitudeStep'],
            -180 + (x+1)*g['longitudeStep'], -90 + (y+1)*g['latitudeStep']]


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def download(url, path):
    """Whole-file downloads with bounded retry; never log authentication headers."""
    path = Path(path)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as response, path.with_suffix('.part').open('wb') as f:
                shutil.copyfileobj(response, f, 1024*1024)
            path.with_suffix('.part').replace(path)
            return
        except Exception:
            if attempt == 3: raise
            time.sleep(2**attempt)


class GitHubAPIError(RuntimeError):
    def __init__(self,status,message):
        self.status=status
        super().__init__(f'GitHub HTTP {status}: {message}')


def github_api(endpoint, method='GET', payload=None, file=None):
    """Use one request per asset; stream bytes and respect the existing token's quota."""
    host='uploads.github.com' if file else 'api.github.com'
    headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'User-Agent':'kaeroute-map-builder',
             'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    encoded=json.dumps(payload).encode() if payload is not None else None
    if file:
        headers.update({'Content-Type':'application/octet-stream','Content-Length':str(Path(file).stat().st_size)})
    elif encoded is not None:
        headers.update({'Content-Type':'application/json','Content-Length':str(len(encoded))})
    deadline=time.monotonic()+4*3600
    transient_failures=0
    for attempt in range(20):
        # With two workflow workers this caps writes at about 450/hour.
        # Pace every attempt, including retries and newly started jobs.
        if method not in ('GET','HEAD'): time.sleep(16)
        connection=http.client.HTTPSConnection(host,timeout=120)
        try:
            with Path(file).open('rb') if file else contextlib.nullcontext(encoded) as body:
                connection.request(method,'/'+endpoint.lstrip('/'),body=body,headers=headers)
                response=connection.getresponse(); raw=response.read()
            try: value=json.loads(raw) if raw else {}
            except (ValueError,UnicodeDecodeError): value={}
            if 200 <= response.status < 300: return value
            message=value.get('message','request failed')
            if response.status in (403,429) and ('rate limit' in message.lower() or response.status==429):
                reset=response.getheader('X-RateLimit-Reset','0')
                after=response.getheader('Retry-After','60')
                delay=max(min(900,60*2**attempt),int(after) if after.isdigit() else 60,
                          int(reset)-int(time.time())+5 if reset.isdigit() else 0)
                if time.monotonic()+delay > deadline: raise GitHubAPIError(response.status,'quota wait exceeded four hours; resume this build later')
                print(f'GitHub quota reached; waiting {delay}s before resuming the same request',flush=True)
                time.sleep(delay); continue
            if response.status >= 500 and transient_failures < 6:
                time.sleep(min(60,2**transient_failures)); transient_failures+=1; continue
            raise GitHubAPIError(response.status,message)
        except (OSError,http.client.HTTPException):
            if transient_failures >= 6: raise
            time.sleep(min(60,2**transient_failures)); transient_failures+=1
        finally:
            connection.close()
    raise RuntimeError('GitHub retry budget exhausted; resume this build later')


_releases={}


def release_info(repo,tag,refresh=False):
    key=(repo,tag)
    if refresh or key not in _releases:
        _releases[key]=github_api(f'repos/{repo}/releases/tags/{urllib.parse.quote(tag,safe="")}')
    return _releases[key]


def release(repo,tag,title):
    try: return release_info(repo,tag)
    except GitHubAPIError as error:
        if error.status != 404: raise
    payload={'tag_name':tag,'name':title,'prerelease':True,
             'body':ATTRIBUTION+'\nOSM-derived data distributed under ODbL 1.0. Preparedness maps do not guarantee safety or access.'}
    try: result=github_api(f'repos/{repo}/releases','POST',payload=payload)
    except GitHubAPIError as error:
        if error.status != 422: raise
        result=release_info(repo,tag,refresh=True)
    _releases[(repo,tag)]=result
    return result


def asset_matches(asset,path):
    known=asset.get('digest')
    if known and known.startswith('sha256:'): return known=='sha256:'+digest(path)
    with urllib.request.urlopen(asset['browser_download_url'],timeout=120) as response:
        h=hashlib.sha256()
        for block in iter(lambda:response.read(1024*1024),b''): h.update(block)
    return h.hexdigest()==digest(path)


def upload(repo,tag,files):
    info=release_info(repo,tag)
    assets={a['name']:a for a in info['assets']}
    for item in files:
        path=Path(item)
        if path.name in assets:
            if not asset_matches(assets[path.name],path): raise ValueError('immutable asset differs: '+path.name)
            continue
        endpoint=f"repos/{repo}/releases/{info['id']}/assets?"+urllib.parse.urlencode({'name':path.name})
        try: asset=github_api(endpoint,'POST',file=path)
        except GitHubAPIError as error:
            # The upload may have completed even if its first response was lost.
            if error.status != 422: raise
            info=release_info(repo,tag,refresh=True)
            asset=next((a for a in info['assets'] if a['name']==path.name),None)
            if not asset or not asset_matches(asset,path): raise
        if asset['size'] != path.stat().st_size or (asset.get('digest') and asset['digest']!='sha256:'+digest(path)):
            raise ValueError('uploaded asset checksum/size differs: '+path.name)
        assets[path.name]=asset
        if not any(a['name']==path.name for a in info['assets']): info['assets'].append(asset)


def download_assets(repo,tag,names,directory):
    info=release_info(repo,tag)
    assets={a['name']:a for a in info['assets']}
    for name in names:
        if name not in assets: raise ValueError('missing release asset: '+name)
        download(assets[name]['browser_download_url'],Path(directory)/name)
    return info


def plan(args):
    import osmium
    work = Path(args.work).resolve(); work.mkdir(parents=True, exist_ok=True)
    g = grid(args.country, args.size)
    # A final source plan exists only after all immutable extracts were uploaded.
    # Resume tile generation without downloading/scanning the entire country.
    tag = f'SOURCE-{args.country}-{args.version}'
    try: existing=release_info(args.repo,tag)
    except GitHubAPIError as error:
        if error.status != 404: raise
        existing=None
    if existing:
        asset=next((a for a in existing['assets'] if a['name']=='plan.json'),None)
        if asset:
            download(asset['browser_download_url'],work/'plan.json')
            previous=json.loads((work/'plan.json').read_text())
            if (previous['version'] != args.version or previous['scope'] != args.scope or previous['grid'] != g
                or any(s['maxZoom'] != args.zoom or s.get('boundarySHA256') != digest(ROOT/'bounds'/f'{args.country}.poly') for s in previous['batches'])):
                raise ValueError('immutable source plan differs from request')
            write(args.output,{'include':[{'batch':s['batch'],'sourceTag':s['sourceTag'],'version':args.version} for s in previous['batches']]})
            print(f"{args.country}: reused {len(previous['batches'])} verified source batch specifications",flush=True)
            return
    source_url = f'https://download.geofabrik.de/asia/{SOURCES[args.country]}-{SOURCE_DATE}.osm.pbf'
    source = work/'source.osm.pbf'
    download(source_url, source)
    md5 = urllib.request.urlopen(source_url + '.md5').read().decode().split()[0]
    if digest(source, 'md5') != md5: raise ValueError('source checksum mismatch')
    source_sha = digest(source)
    # Extract's bitsets scale with the largest ID, not the number of local
    # objects. Dense derived IDs keep smart multi-extracts within runner RAM.
    dense = work/'dense.osm.pbf'
    run('osmium','renumber',source,'-o',dense,'--overwrite')
    source.unlink(); dense.replace(source)
    dense_sha = digest(source)
    occupied = set()
    class Nodes(osmium.SimpleHandler):
        def node(self, node):
            if node.location.valid():
                x = math.floor((node.location.lon+180)/g['longitudeStep'])
                y = math.floor((node.location.lat+90)/g['latitudeStep'])
                occupied.add((x,y))
    Nodes().apply_file(str(source))
    from shapely.geometry import box
    from shapely.prepared import prep
    extent = prep(boundary(args.country))
    # Complete OSM relations may contain nodes thousands of kilometres outside
    # the extract. They are geometry context, not supported download coverage.
    occupied = {(x,y) for x,y in occupied if extent.intersects(box(*cell(x,y,g)))}
    # A full cell of padding provides coastal context and covers the 2 km PoC buffer.
    cells = {(x+dx,y+dy) for x,y in occupied for dx in (-1,0,1) for dy in (-1,0,1)}
    if args.scope == 'poc':
        # Geographic evaluation areas; these are NOT fabricated ORS routes.
        bbox = [139.25,35.40,139.85,35.85] if args.country == 'JP' else [121.20,24.90,121.65,25.20]
        cells = {(x,y) for x,y in cells if intersects(cell(x,y,g), bbox)}
    batches = {}
    for x,y in sorted(cells): batches.setdefault((x//6,y//6),[]).append((x,y))
    specs = []
    for (bx,by), members in sorted(batches.items()):
        batch = f'{args.country}-{bx}-{by}'
        b = cell(bx*6,by*6,g); upper = cell(bx*6+5,by*6+5,g); b[2:]=upper[2:]
        # Keep complete ways/multipolygons in the extraction; do not cut at administrative borders.
        extraction = [b[0]-.03,b[1]-.03,b[2]+.03,b[3]+.03]
        specs.append({'batch':batch,'country':args.country,'grid':g,'bounds':b,'extractionBounds':extraction,
                      'cells':[{'id':f'{args.country}-{x}-{y}','bounds':cell(x,y,g)} for x,y in members],
                      'sourceURL':source_url,'sourceSHA256':source_sha,'sourceDate':SOURCE_DATE,
                      'boundarySHA256':digest(ROOT/'bounds'/f'{args.country}.poly'),
                      'derivedIDs':'dense, not original OSM IDs; never upload to OSM','denseSourceSHA256':dense_sha,
                      'mapVersion':args.version,'minZoom':8,'maxZoom':args.zoom,'planetilerVersion':PLANETILER_VERSION})
    tag = f'SOURCE-{args.country}-{args.version}'
    release(args.repo,tag,tag)
    for i,spec in enumerate(specs): spec['sourceTag']=f'{tag}-part{i//400+1:03d}'
    for part in sorted({s['sourceTag'] for s in specs}): release(args.repo,part,part)
    write(work/'coverage-plan.json',{'country':args.country,'gridCount':len(cells),'batchCount':len(specs),
                                    'version':args.version,'grid':g,'batches':[s['batch'] for s in specs]})
    upload(args.repo,tag,[work/'coverage-plan.json'])
    print(f'{args.country}: {len(cells)} grids, {len(specs)} batches, extracting dense-ID inputs',flush=True)
    # Limit the number of simultaneous extracts to keep osmium's indexes bounded.
    for offset in range(0,len(specs),12):
        chunk = specs[offset:offset+12]
        config = {'extracts':[{'output': str(work/(s['batch']+'.osm.pbf')), 'bbox':s['extractionBounds']} for s in chunk]}
        write(work/'extracts.json',config)
        run('osmium','extract','-c',work/'extracts.json','-s','smart','-S','types=multipolygon',source,'--overwrite')
        for spec in chunk:
            pbf=work/(spec['batch']+'.osm.pbf'); spec['extractedSHA256']=digest(pbf)
            write(work/(spec['batch']+'.json'),spec)
            upload(args.repo,spec['sourceTag'],[pbf,work/(spec['batch']+'.json')])
            pbf.unlink()
        print(f'{args.country}: published {min(offset+12,len(specs))}/{len(specs)} source batches',flush=True)
    write(work/'plan.json',{'country':args.country,'version':args.version,'scope':args.scope,'grid':g,'batches':specs})
    upload(args.repo,tag,[work/'plan.json'])
    output = {'include':[{'batch':s['batch'],'sourceTag':s['sourceTag'],'version':args.version} for s in specs]}
    write(args.output,output)
    print(f'{args.country}: {len(cells)} grids, {len(specs)} build jobs')


def intersects(a,b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def empty_tile(compression):
    import gzip
    from pmtiles.tile import Compression
    if compression == Compression.GZIP: return gzip.compress(b'',mtime=0)
    if compression == Compression.NONE: return b''
    raise ValueError('unsupported empty tile compression')


def boundary(country):
    """Pinned Geofabrik extraction extent, including islands and polygon holes."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    lines = iter((ROOT/'bounds'/f'{country}.poly').read_text().splitlines()[1:])
    positive, negative = [], []
    for label in lines:
        label = label.strip()
        if label == 'END': break
        if not label: continue
        coordinates = []
        for line in lines:
            if line.strip() == 'END': break
            coordinates.append(tuple(map(float,line.split())))
        polygon = Polygon(coordinates)
        if not polygon.is_valid: raise ValueError('invalid extraction boundary')
        (negative if label.startswith('!') else positive).append(polygon)
    if not positive: raise ValueError('empty extraction boundary')
    return unary_union(positive).difference(unary_union(negative))


def build(args):
    from pmtiles.reader import Reader, MmapSource, all_tiles
    from pmtiles.writer import Writer
    from pmtiles.tile import zxy_to_tileid
    import mmap
    work=Path(args.work).resolve(); work.mkdir(parents=True,exist_ok=True)
    download_assets(args.repo,args.source_tag,[args.batch+'.json',args.batch+'.osm.pbf'],work)
    spec=json.loads((work/(args.batch+'.json')).read_text())
    pbf=work/(args.batch+'.osm.pbf')
    if digest(pbf)!=spec['extractedSHA256']:raise ValueError('extract checksum mismatch')
    tag=f"{args.batch}-{spec['mapVersion']}"
    try: published=release_info(args.repo,tag)
    except GitHubAPIError as error:
        if error.status != 404: raise
        published=None
    if published and any(a['name']=='manifest-fragment.json' for a in published['assets']):
        download_assets(args.repo,tag,['manifest-fragment.json'],work)
        fragment=json.loads((work/'manifest-fragment.json').read_text())
        validate_manifest(fragment)
        if (fragment['mapVersion']!=spec['mapVersion'] or fragment['sourceSHA256']!=spec['sourceSHA256']
            or fragment['planetilerSHA256']!=PLANETILER_SHA256 or fragment['grids'][spec['country']]!=spec['grid']
            or set(fragment['packages'])!={c['id'] for c in spec['cells']}):
            raise ValueError('immutable published batch differs')
        assets={a['name']:a for a in published['assets']}
        if all(assets.get(key+'.pmtiles',{}).get('size')==value['size'] and assets.get(key+'.pmtiles',{}).get('digest')=='sha256:'+value['checksum'] for key,value in fragment['packages'].items()):
            print(json.dumps({'batch':args.batch,'reused':True,'files':len(fragment['packages'])}),flush=True)
            return
    jar=ROOT/'.cache'/f'planetiler-{PLANETILER_VERSION}.jar';jar.parent.mkdir(exist_ok=True)
    if not jar.exists(): download(f'https://github.com/onthegomap/planetiler/releases/download/v{PLANETILER_VERSION}/planetiler.jar',jar)
    if digest(jar) != PLANETILER_SHA256: raise ValueError('Planetiler checksum mismatch')
    started=time.monotonic()
    out=work/'batch.pmtiles'
    run('java','-Xmx4g','-jar',jar,ROOT/'profile.yml',f'--osm-path={pbf}',
        '--osm-url=file://'+str(pbf.resolve()),f'--output={out}',f'--tmpdir={work}/temp',
        '--minzoom=8',f"--maxzoom={spec['maxZoom']}",
        '--bounds='+','.join(str(v) for v in spec['extractionBounds']), '--force')
    pbf.unlink(); shutil.rmtree(work/'temp',ignore_errors=True)
    entries={}
    tag=f"{args.batch}-{spec['mapVersion']}"
    release(args.repo,tag,tag)
    # Stream each tile once, assigning it to intersecting grid files. A complete
    # tile is retained at edges, so roads/labels are not truncated at cell borders.
    writers={}; handles={}
    for c in spec['cells']:
        handles[c['id']]=(work/(c['id']+'.pmtiles')).open('wb'); writers[c['id']]=Writer(handles[c['id']])
    with out.open('rb') as source, mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ) as mapped:
        src=lambda offset,length: mapped[offset:offset+length]; reader=Reader(src); header=reader.header(); metadata=reader.metadata()
        for (z,x,y),data in all_tiles(src):
            n=2**z
            def latitude(y):return math.degrees(math.atan(math.sinh(math.pi*(1-2*y/n))))
            tilebox=[x/n*360-180,latitude(y+1),(x+1)/n*360-180,latitude(y)]
            for c in spec['cells']:
                if intersects(tilebox,c['bounds']):writers[c['id']].write_tile(zxy_to_tileid(z,x,y),data)
        for c in spec['cells']:
            h=dict(header)
            for field,value in zip(['min_lon_e7','min_lat_e7','max_lon_e7','max_lat_e7'],c['bounds']):h[field]=round(value*1e7)
            # Retain the advertised zoom range even for uninhabited coastal cells.
            import gzip
            from unittest.mock import patch
            w=writers[c['id']]
            for z in (8,spec['maxZoom']):
                if not any(e.tile_id >= zxy_to_tileid(z,0,0) and e.tile_id < zxy_to_tileid(z+1,0,0) for e in w.tile_entries):
                    lon=(c['bounds'][0]+c['bounds'][2])/2;lat=(c['bounds'][1]+c['bounds'][3])/2
                    x=int((lon+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**z)
                    empty=empty_tile(h['tile_compression'])
                    w.write_tile(zxy_to_tileid(z,x,y),empty)
            compress=gzip.compress
            with patch('gzip.compress',side_effect=lambda data, *a, **kw: compress(data,mtime=0)):
                w.finalize(h,dict(metadata,attribution=ATTRIBUTION))
            handles[c['id']].close()
    for c in spec['cells']:
        path=work/(c['id']+'.pmtiles')
        if path.stat().st_size>=2_147_483_648:raise ValueError('release asset too large')
        entries[c['id']]={'size':path.stat().st_size,'url':f'https://github.com/{args.repo}/releases/download/{tag}/{path.name}',
                           'checksum':digest(path),'bounds':c['bounds'],'country':spec['country'],'region':args.batch,
                           'minZoom':8,'maxZoom':spec['maxZoom'],'schema':'kaeroute-v1','attribution':ATTRIBUTION}
    report={'schemaVersion':1,'mapVersion':spec['mapVersion'],'grids':{spec['country']:spec['grid']},'packages':entries,
            'sourceURL':spec['sourceURL'],'sourceSHA256':spec['sourceSHA256'],'planetilerVersion':PLANETILER_VERSION,
            'planetilerSHA256':PLANETILER_SHA256}
    write(work/'manifest-fragment.json',report)
    upload(args.repo,tag,[work/(c['id']+'.pmtiles') for c in spec['cells']]+[work/'manifest-fragment.json'])
    print(json.dumps({'batch':args.batch,'files':len(entries),'bytes':sum(p['size'] for p in entries.values()),
                      'maxBytes':max(p['size'] for p in entries.values()),'seconds':round(time.monotonic()-started,2)}))


def assemble(args):
    manifest={'schemaVersion':1,'mapVersion':args.version,'grids':{},'packages':{}}
    expected=set()
    for country in SOURCES:
        directory=Path(args.work)/country;directory.mkdir(parents=True,exist_ok=True)
        download_assets(args.repo,f'SOURCE-{country}-{args.version}',['plan.json'],directory)
        plan=json.loads((directory/'plan.json').read_text())
        if plan['scope'] != args.scope:raise ValueError('scope mismatch')
        for spec in plan['batches']:
            tag=f"{spec['batch']}-{args.version}"
            target=directory/spec['batch'];target.mkdir(exist_ok=True)
            info=download_assets(args.repo,tag,['manifest-fragment.json'],target)
            fragment=json.loads((target/'manifest-fragment.json').read_text())
            if fragment['mapVersion']!=args.version or fragment['grids'][country]!=plan['grid']:raise ValueError('mixed version/grid')
            ids={c['id'] for c in spec['cells']}
            if set(fragment['packages'])!=ids:raise ValueError('incomplete batch')
            if expected & ids:raise ValueError('duplicate grid')
            expected |= ids
            # Confirm all published assets and sizes before activating the catalog.
            assets=info['assets']
            sizes={a['name']:a['size'] for a in assets}
            checksums={a['name']:a.get('digest') for a in assets}
            for key,value in fragment['packages'].items():
                if sizes.get(key+'.pmtiles')!=value['size']:raise ValueError('missing/wrong-size asset')
                if checksums.get(key+'.pmtiles') and checksums[key+'.pmtiles']!='sha256:'+value['checksum']:raise ValueError('uploaded checksum mismatch')
            manifest['packages'].update(fragment['packages']);manifest['grids'].update(fragment['grids'])
    if not expected:raise ValueError('empty catalog')
    validate_manifest(manifest)
    write(args.output,manifest)
    report={'scope':args.scope,'version':args.version,'packages':len(expected),
            'bytes':sum(v['size'] for v in manifest['packages'].values()),
            'maxBytes':max(v['size'] for v in manifest['packages'].values()),
            'orsRouteValidation':'pending: API key required','deviceValidation':'pending'}
    write(Path(args.output).with_name('generation-report.json'),report)
    print(json.dumps(report))


def validate_manifest(m):
    if m['schemaVersion']!=1 or not m['packages']:raise ValueError('invalid manifest')
    for key,p in m['packages'].items():
        country,x,y=key.split('-');x=int(x);y=int(y)
        if country!=p['country'] or country not in SOURCES:raise ValueError('country mismatch')
        expected=cell(x,y,m['grids'][country])
        if any(abs(a-b)>1e-7 for a,b in zip(expected,p['bounds'])) or len(p['bounds'])!=4:raise ValueError('grid bounds mismatch')
        if not 127 < p['size'] < 2_147_483_648:raise ValueError('invalid size')
        if not re.fullmatch('[a-f0-9]{64}',p['checksum']):raise ValueError('invalid checksum')
        if not p['url'].startswith('https://'):raise ValueError('non-HTTPS URL')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['plan','build','assemble'])
    parser.add_argument('--repo',default='moris94/kaeroute-geomap');parser.add_argument('--work',default='work')
    parser.add_argument('--country',choices=list(SOURCES));parser.add_argument('--version',default='2026-09-poc1')
    parser.add_argument('--scope',choices=['poc','full'],default='poc');parser.add_argument('--size',type=int,choices=[10,15,20],default=15)
    parser.add_argument('--zoom',type=int,choices=[14,15,16],default=16);parser.add_argument('--output',default='matrix.json')
    parser.add_argument('--batch');parser.add_argument('--source-tag')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}',args.version):parser.error('invalid version')
    globals()[args.command](args)


if __name__=='__main__':main()
