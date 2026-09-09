import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pipeline',ROOT/'pipeline.py')
pipeline=importlib.util.module_from_spec(spec);spec.loader.exec_module(pipeline)

class PipelineTests(unittest.TestCase):
    def test_empty_tile_uses_pmtiles_compression_enum(self):
        import gzip
        from pmtiles.tile import Compression
        self.assertEqual(gzip.decompress(pipeline.empty_tile(Compression.GZIP)),b'')
        self.assertEqual(pipeline.empty_tile(Compression.NONE),b'')
        self.assertEqual(pipeline.empty_tile(Compression.GZIP),pipeline.empty_tile(Compression.GZIP))
        with self.assertRaises(ValueError):pipeline.empty_tile(Compression.UNKNOWN)
    def example(self):
        g=pipeline.grid('JP');x,y=1920,932
        return {'schemaVersion':1,'mapVersion':'2026-09-test','grids':{'JP':g},'packages':{
            f'JP-{x}-{y}':{'country':'JP','bounds':pipeline.cell(x,y,g),'size':1000,
                           'url':'https://example.com/grid.pmtiles','checksum':'a'*64}}}
    def test_grid_neighbors_have_identical_edges(self):
        for country in ('JP','TW'):
            for size in (10,15,20):
                g=pipeline.grid(country,size)
                self.assertEqual(pipeline.cell(100,200,g)[2],pipeline.cell(101,200,g)[0])
                self.assertEqual(pipeline.cell(100,200,g)[3],pipeline.cell(100,201,g)[1])
    def test_manifest_rejects_missing_or_displaced_coverage(self):
        m=self.example();pipeline.validate_manifest(m)
        bad=copy.deepcopy(m);next(iter(bad['packages'].values()))['bounds'][0]+=.001
        with self.assertRaises(ValueError):pipeline.validate_manifest(bad)
        bad=copy.deepcopy(m);bad['packages']={}
        with self.assertRaises(ValueError):pipeline.validate_manifest(bad)
    def test_manifest_rejects_corrupt_file_metadata(self):
        for field,value in [('size',127),('size',2**31),('checksum','invalid'),('url','http://example.com/file'),('country','TW')]:
            m=self.example();next(iter(m['packages'].values()))[field]=value
            with self.assertRaises(ValueError):pipeline.validate_manifest(m)
    def test_intersection_includes_touching_grid_boundary(self):
        self.assertTrue(pipeline.intersects([0,0,1,1],[1,1,2,2]))
        self.assertFalse(pipeline.intersects([0,0,1,1],[1.001,1.001,2,2]))
    def test_streaming_digest(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'data';data=b'offline'*200000;p.write_bytes(data)
            self.assertEqual(pipeline.digest(p),hashlib.sha256(data).hexdigest())
    def test_country_extent_includes_islands_and_excludes_relation_outliers(self):
        from shapely.geometry import Point
        for country,points in [('JP',[(139.77,35.68),(141.67,45.41),(127.68,26.21),(124.16,24.34)]),
                               ('TW',[(121.52,25.05),(120.30,22.64),(119.57,23.57),(118.32,24.43)])]:
            extent=pipeline.boundary(country)
            self.assertTrue(all(extent.covers(Point(*p)) for p in points))
        self.assertFalse(pipeline.boundary('TW').covers(Point(120,34)))
        self.assertFalse(pipeline.boundary('JP').covers(Point(0,0)))
    def test_profile_schema_and_dependencies(self):
        import yaml
        value=yaml.safe_load((ROOT/'profile.yml').read_text())
        layers={v['id'] for v in value['layers']}
        self.assertTrue({'transportation','railway','water','waterway','building','landmark','place'}<=layers)
        self.assertEqual(value['sources']['osm']['type'],'osm')

if __name__=='__main__':unittest.main()
