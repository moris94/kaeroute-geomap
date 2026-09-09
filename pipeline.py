#!/usr/bin/env python3
"""Reproducible Geofabrik -> Planetiler -> grid PMTiles release pipeline.

Only generated public geographic data is uploaded. Never include routes, user
locations, application sources, API keys, or local config in release assets.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

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


def release(repo, tag, title):
    check = subprocess.run(['gh', 'release', 'view', tag, '--repo', repo], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        run('gh', 'release', 'create', tag, '--repo', repo, '--title', title,
            '--notes', ATTRIBUTION + '\nOSM-derived data distributed under ODbL 1.0. Preparedness maps do not guarantee safety or access.', '--prerelease')


def upload(repo, tag, files):
    # Never clobber an existing asset. Resuming requires content equality.
    entries = json.loads(subprocess.check_output(['gh', 'api', f'repos/{repo}/releases/tags/{tag}']))
    assets = {a['name']: a for a in entries['assets']}
    for path in files:
        path = Path(path)
        if path.name in assets:
            known_digest = assets[path.name].get('digest')
            if known_digest and known_digest.startswith('sha256:'):
                if known_digest != 'sha256:'+digest(path): raise ValueError('immutable asset differs: '+path.name)
                continue
            with urllib.request.urlopen(assets[path.name]['browser_download_url']) as response:
                h = hashlib.sha256()
                for block in iter(lambda: response.read(1024*1024), b''): h.update(block)
            if h.hexdigest() != digest(path): raise ValueError('immutable asset differs: ' + path.name)
        else:
            run('gh', 'release', 'upload', tag, path, '--repo', repo)


def plan(args):
    import osmium
    work = Path(args.work).resolve(); work.mkdir(parents=True, exist_ok=True)
    g = grid(args.country, args.size)
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
    run('gh','release','download',args.source_tag,'--repo',args.repo,'--pattern',args.batch+'.*','--dir',work,'--clobber')
    spec=json.loads((work/(args.batch+'.json')).read_text())
    pbf=work/(args.batch+'.osm.pbf')
    if digest(pbf)!=spec['extractedSHA256']:raise ValueError('extract checksum mismatch')
    jar=work/'planetiler.jar'
    download(f'https://github.com/onthegomap/planetiler/releases/download/v{PLANETILER_VERSION}/planetiler.jar',jar)
    if digest(jar) != PLANETILER_SHA256: raise ValueError('Planetiler checksum mismatch')
    started=time.monotonic()
    out=work/'batch.pmtiles'
    run('java','-Xmx4g','-jar',jar,ROOT/'profile.yml',f'--osm-path={pbf}',
        '--osm-url=file://'+str(pbf.resolve()),f'--output={out}',f'--tmpdir={work}/temp',
        '--minzoom=8',f"--maxzoom={spec['maxZoom']}",
        '--bounds='+','.join(str(v) for v in spec['extractionBounds']), '--force')
    pbf.unlink(); jar.unlink(); shutil.rmtree(work/'temp',ignore_errors=True)
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
                    empty=gzip.compress(b'',mtime=0) if int(h['tile_compression'])==2 else b''
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
        run('gh','release','download',f'SOURCE-{country}-{args.version}','--repo',args.repo,'--pattern','plan.json','--dir',directory,'--clobber')
        plan=json.loads((directory/'plan.json').read_text())
        if plan['scope'] != args.scope:raise ValueError('scope mismatch')
        for spec in plan['batches']:
            tag=f"{spec['batch']}-{args.version}"
            target=directory/spec['batch'];target.mkdir(exist_ok=True)
            run('gh','release','download',tag,'--repo',args.repo,'--pattern','manifest-fragment.json','--dir',target,'--clobber')
            fragment=json.loads((target/'manifest-fragment.json').read_text())
            if fragment['mapVersion']!=args.version or fragment['grids'][country]!=plan['grid']:raise ValueError('mixed version/grid')
            ids={c['id'] for c in spec['cells']}
            if set(fragment['packages'])!=ids:raise ValueError('incomplete batch')
            if expected & ids:raise ValueError('duplicate grid')
            expected |= ids
            # Confirm all published assets and sizes before activating the catalog.
            assets=json.loads(subprocess.check_output(['gh','api',f'repos/{args.repo}/releases/tags/{tag}']))['assets']
            sizes={a['name']:a['size'] for a in assets}
            for key,value in fragment['packages'].items():
                if sizes.get(key+'.pmtiles')!=value['size']:raise ValueError('missing/wrong-size asset')
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
