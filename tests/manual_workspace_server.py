"""Isolated browser-test server. All optimizer/Trial calls are controlled fixtures.
No production database or model provider is used. Stop with Ctrl+C.
"""
import sys,time
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_improvement as helpers
from evo_arena.improvement import ImprovementExperiment
from evo_arena.web import serve
f=helpers.ImprovementTests();f.setUp()
Base=f.runner()
class SlowTrial(Base):
    def execute(self,*args,**kwargs):
        time.sleep(.08)
        return super().execute(*args,**kwargs)
def prepare(self, ex, selected):return f.fake_prepare(ex,selected)
try:
    with patch.object(ImprovementExperiment,'prepare',prepare),patch.object(ImprovementExperiment,'optimizer_factory',staticmethod(f.optimizer)),patch('evo_arena.evolution.HarborTrial',SlowTrial):
        serve(f.store,f.settings,port=8795,harbor_bin=f.harbor)
finally:f.tearDown()
