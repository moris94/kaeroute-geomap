#!/usr/bin/env python3
"""Compare actual PMTiles bytes for nine grid/zoom choices on fixed OSM areas.

Geographic areas only: this is not a substitute for ORS route acceptance tests.
Existing PoC extracts are reused; no user locations or routes are uploaded.
"""
import argparse
import gzip
import json
import math
import mmap
from pathlib import Path
from unittest.mock import patch
from pmtiles.reader import Reader, all_tiles
from pmtiles.writer import Writer
from pmtiles.tile import zxy_to_tileid
from pipeline import (ROOT, PLANETILER_VERSION, PLANETILER_SHA256, ATTRIBUTION,
                      grid, cell, intersects, download, digest, run, write)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--country', choices=['JP', 'TW'], required=True)
    parser.add_argument('--repo', default='moris94/kaeroute-geomap')
    args = parser.parse_args()
    work = Path('benchmark-work').resolve(); work.mkdir(exist_ok=True)
    tag = f'SOURCE-{args.country}-2026-09-poc1'
    run('gh', 'release', 'download', tag, '--repo', args.repo, '--pattern', '*.osm.pbf', '--pattern', 'plan.json', '--dir', work)
    plan = json.loads((work/'plan.json').read_text())
    for spec in plan['batches']:
        if digest(work/(spec['batch']+'.osm.pbf')) != spec['extractedSHA256']:
            raise ValueError('source extract checksum mismatch')
    pbf = work/'combined.pbf'
    run('osmium', 'merge', *sorted(work.glob('*.osm.pbf')), '-o', pbf)
    bbox = [139.25,35.40,139.85,35.85] if args.country == 'JP' else [121.20,24.90,121.65,25.20]
    # Generate enough surrounding data to measure all complete intersecting grids,
    # including their edge tiles at z8 (roughly 140 km across).
    bounds = [bbox[0]-1.5,bbox[1]-1.5,bbox[2]+1.5,bbox[3]+1.5]
    jar = work/'planetiler.jar'
    download(f'https://github.com/onthegomap/planetiler/releases/download/v{PLANETILER_VERSION}/planetiler.jar', jar)
    if digest(jar) != PLANETILER_SHA256: raise ValueError('Planetiler checksum mismatch')
    run('java', '-Xmx4g', '-jar', jar, ROOT/'profile.yml', f'--osm-path={pbf}',
        '--osm-url=file://'+str(pbf), f'--output={work}/area.pmtiles', f'--tmpdir={work}/temp',
        '--minzoom=8', '--maxzoom=16', '--bounds='+','.join(map(str,bounds)), '--force')
    results = []
    with (work/'area.pmtiles').open('rb') as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as mapped:
        src = lambda offset,length: mapped[offset:offset+length]
        reader = Reader(src)
        for size in (10,15,20):
            g = grid(args.country,size)
            # Same fixed area in all variants; all intersecting whole grids count.
            cells = [(x,y,cell(x,y,g))
                     for x in range(math.floor((bbox[0]+180)/g['longitudeStep']),math.floor((bbox[2]+180)/g['longitudeStep'])+1)
                     for y in range(math.floor((bbox[1]+90)/g['latitudeStep']),math.floor((bbox[3]+90)/g['latitudeStep'])+1)]
            for zoom in (14,15,16):
                files = [(work/f'{x}-{y}.pmtiles').open('wb') for x,y,b in cells]
                writers = [Writer(f) for f in files]
                for (z,x,y),data in all_tiles(src):
                    if z > zoom: continue
                    n = 2**z
                    latitude = lambda t: math.degrees(math.atan(math.sinh(math.pi*(1-2*t/n))))
                    box = [x/n*360-180,latitude(y+1),(x+1)/n*360-180,latitude(y)]
                    for (_,_,b),w in zip(cells,writers):
                        if intersects(box,b): w.write_tile(zxy_to_tileid(z,x,y),data)
                sizes=[]
                for (_,_,b),w,f in zip(cells,writers,files):
                    h = dict(reader.header())
                    for field,value in zip(['min_lon_e7','min_lat_e7','max_lon_e7','max_lat_e7'],b): h[field]=round(value*1e7)
                    compress = gzip.compress
                    with patch('gzip.compress',side_effect=lambda data,*a,**kw:compress(data,mtime=0)):
                        w.finalize(h,dict(reader.metadata(),attribution=ATTRIBUTION))
                    f.close(); path=Path(f.name); sizes.append(path.stat().st_size);path.unlink()
                results.append({'gridKM':size,'maxZoom':zoom,'packages':len(sizes),'totalBytes':sum(sizes),
                                'maxBytes':max(sizes),'medianBytes':sorted(sizes)[len(sizes)//2]})
    write('benchmark-'+args.country+'.json', {'country':args.country,'bounds':bbox,
          'sourceVersion':'2026-09-poc1','scope':'fixed geographic area, not route validation',
          'edgeNote':'Low zoom context is limited to the published source extracts; high zoom cells are complete.',
          'results':results})
    print(json.dumps(results))

if __name__ == '__main__': main()
