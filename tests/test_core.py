import unittest,sys,json,copy,importlib.util
sys.path.insert(0,'src')
from core import *
from main import synthetic_pool,run_day,summarize

class TestCore(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads(Path('config.json').read_text()); self.pool=synthetic_pool(self.cfg)
        self.rows=make_requests(self.pool,self.cfg,'normal',11)
        self.rs=[r for r in self.rows if r['direction']=='TB-HN' and r['slot_sec']==4*3600]
        deps={p['city']:(p['route_lon'],p['route_lat']) for p in self.pool if p['kind']=='depot'}
        self.P=build_problem(self.rs,'TB','HN',deps,self.cfg,'synthetic',1,4*3600)
    def test_reproducible(self): self.assertEqual(self.rows,make_requests(self.pool,self.cfg,'normal',11))
    def test_pairs(self):
        stops=stops_csv(self.rows); self.assertEqual(len(stops),2*len(self.rows)); self.assertEqual(sum(x['seats_delta'] for x in stops),0)
    def test_pickup_first(self):
        p,q=self.P['pairs'][0];self.assertIsNone(evaluate(self.P,[0,q,p,len(self.P['nodes'])-1]))
    def test_no_half_request(self):
        p,q=self.P['pairs'][0]; self.assertIsNone(evaluate(self.P,[0,p,len(self.P['nodes'])-1]))
    def test_capacity(self):
        p,q=self.P['pairs'][0];self.P['capacity']=0;self.assertIsNone(evaluate(self.P,[0,p,q,len(self.P['nodes'])-1]))
    def test_greedy_feasible(self):
        r=greedy(self.P,self.rs); self.assertIsNotNone(evaluate(self.P,r['route'],r['times']))
    def test_fleet(self):
        t,p,r=run_day(self.cfg,self.rows,self.pool,'greedy','synthetic',1,15);self.assertEqual(len(t),30);self.assertEqual(len(p),len(self.rows));self.assertEqual(len({a['request_id'] for a in p}),len(p))
        prev={}
        for x in t:
            if x['departed']:
                v=x['vehicle_id'];a,b=x['direction'].split('-');self.assertEqual(a,prev.get(v,(v[:2],0))[0]);self.assertGreaterEqual(x['slot_sec'],prev.get(v,(a,0))[1]);prev[v]=(b,x['ready_sec'])
        self.assertLessEqual(len(prev),6)
    @unittest.skipUnless(importlib.util.find_spec('ortools'),'OR-Tools not installed')
    def test_ortools(self):
        r,status=optimize(self.P,self.rs,1);self.assertIsNotNone(evaluate(self.P,r['route'],r['times']));self.assertGreaterEqual(len(r['served']),len(greedy(self.P,self.rs)['served']))
if __name__=='__main__':unittest.main()
