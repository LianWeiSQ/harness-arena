import unittest
from unittest.mock import patch

import test_improvement as helpers
from evo_arena.campaigns import Campaign, cancel_campaign
from evo_arena.projects import create_project, project_view
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena import execution_control as control
from evo_arena.web import recover


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.f=helpers.ImprovementTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.project=create_project(self.f.store,self.f.registry,{"name":"Orders","goal":"Reliable orders","baseline":self.f.baseline["id"],"target":"policy.md"},"p")
        self.engine=Campaign(self.f.store,self.f.registry,self.f.harbor,"test-image")
        self.payload={"project":self.project["id"],"max_rounds":3,"no_gain_limit":2,"limits":{"calls":1000,"tokens":100000,"seconds":600}}

    def run_plan(self, runner=None):
        plan,_=self.engine.create(self.payload,'plan')
        with patch.object(self.engine.engine,'prepare',self.f.fake_prepare),patch.object(self.engine.engine,'optimizer_factory',self.f.optimizer),patch('evo_arena.evolution.HarborTrial',runner or self.f.runner()):
            self.engine.execute(plan)
        return self.f.store.get('evo_campaign',plan['id'])

    def test_successive_generations_share_budget_and_keep_initial_baseline(self):
        p=self.run_plan();self.assertEqual(p['status'],'completed');self.assertEqual(p['reason'],'round_limit')
        self.assertEqual(len(p['experiments']),3);self.assertEqual(p['budget']['calls'],96)
        self.assertEqual(p['budget']['tokens'],1020)
        project=self.f.store.get('evo_project',self.project['id']);self.assertEqual(project['revision'],3)
        self.assertEqual(project['initial_version'],self.f.baseline['id'])
        self.assertEqual(project_view(self.f.store,project)['cost']['calls'],96)
        ex=[self.f.store.get('evo_experiment',i) for i in p['experiments']]
        self.assertEqual(ex[1]['baseline'],ex[0]['candidate']);self.assertEqual(ex[2]['baseline'],ex[1]['candidate'])
        self.assertTrue(all(self.f.store.get('evo_budget',e['id'])['deadline']<=p['budget']['deadline'] for e in ex))
        self.assertEqual(self.engine.create(self.payload,'plan')[0]['id'],p['id'])

    def test_consecutive_no_gain_keeps_parent_and_all_candidates(self):
        self.f.behavior='unchanged';self.payload['max_rounds']=5
        p=self.run_plan();self.assertEqual(p['reason'],'no_gain_limit');self.assertEqual(len(p['experiments']),2)
        self.assertEqual(self.f.store.get('evo_project',self.project['id'])['current_version'],self.f.baseline['id'])
        self.assertEqual(len(self.f.store.list('evo_decision')),2)

    def test_global_call_limit_stops_second_round_without_resetting(self):
        self.payload['limits']['calls']=33
        p=self.run_plan();self.assertEqual(p['status'],'stopped');self.assertEqual(p['budget']['calls'],33)
        self.assertEqual(p['budget']['pending'],0);self.assertEqual(len(p['experiments']),2)
        self.assertEqual(self.f.store.get('evo_project',self.project['id'])['revision'],1)

    def test_unknown_usage_stops_entire_plan(self):
        class Unknown:
            def __init__(self,*a):pass
            def execute(self,run,root,original,record,version,case,budget):
                call=budget.claim(run['id'],'execution','test');budget.settle(call)
                run.update(status='error',error='unknown usage')
        p=self.run_plan(Unknown);self.assertEqual(p['status'],'stopped')
        self.assertEqual(p['budget']['calls'],1);self.assertEqual(p['budget']['unknown_usage_calls'],1)
        self.assertEqual(len(p['experiments']),1)

    def test_runtime_snapshot_is_reused_between_rounds(self):
        seen=[]
        def prepare(ex, selected):
            seen.append((ex.get('image_id'), ex.get('harbor_version')))
            ex.update(image_id='sha256:fixed-image',harbor_version='fixed-harbor')
            return self.f.fake_prepare(ex,selected)
        plan,_=self.engine.create(self.payload,'plan')
        with patch.object(self.engine.engine,'prepare',prepare),patch.object(self.engine.engine,'optimizer_factory',self.f.optimizer),patch('evo_arena.evolution.HarborTrial',self.f.runner()):
            self.engine.execute(plan)
        self.assertEqual(seen,[(None,None),('sha256:fixed-image','fixed-harbor'),('sha256:fixed-image','fixed-harbor')])

    def test_token_threshold_counts_settled_usage_and_prevents_next_dispatch(self):
        self.payload['limits']['tokens']=21
        plan=self.run_plan()
        self.assertEqual(plan['status'],'stopped')
        self.assertEqual(plan['budget']['tokens'],30)
        self.assertEqual(plan['budget']['calls'],3)
        call=self.f.store.list('evo_call')[0]
        ledger=ExperimentBudget(self.f.store,call['experiment'])
        ledger.settle(call['id'],{'total_tokens':10})
        self.assertEqual(self.f.store.get('evo_budget',plan['id'])['tokens'],30)

    def test_cancel_after_round_commit_preserves_result_but_stops_next_round(self):
        original=self.engine.engine.execute
        def cancel_after(ex, bindings):
            original(ex, bindings)
            cancel_campaign(self.f.store,ex['campaign'])
        with patch.object(self.engine.engine,'execute',cancel_after):
            plan=self.run_plan()
        self.assertEqual(plan['status'],'cancelled');self.assertEqual(len(plan['experiments']),1)
        self.assertEqual(self.f.store.get('evo_project',self.project['id'])['revision'],1)

    def test_cancel_blocks_new_calls_and_never_promotes(self):
        f=self.f
        class Cancel:
            def __init__(self,*a):pass
            def execute(self,run,root,original,record,version,case,budget):
                call=budget.claim(run['id'],'execution','test');cancel_campaign(f.store,budget.parent)
                budget.settle(call,{'total_tokens':10})
                budget.check_control()
        p=self.run_plan(Cancel);self.assertEqual(p['status'],'cancelled');self.assertEqual(p['budget']['calls'],1)
        self.assertEqual(self.f.store.get('evo_project',self.project['id'])['revision'],0)

    def test_recovery_settles_parent_unknown_and_prevents_independent_child_resume(self):
        p,_=self.engine.create(self.payload,'plan');p['status']='running';self.f.store.update('evo_campaign',p['id'],p)
        ExperimentBudget(self.f.store,p['id']).start(p['limits'])
        payload={**self.f.payload,'project':p['project'],'stage':'full','limits':p['round_limits'],'campaign':p['id'],'round':1,'optimizer_profile':p['optimizer_profile']}
        ex,_,_=self.engine.engine.create(payload,'child');control.acquire(self.f.store,ex)
        ledger=ExperimentBudget(self.f.store,ex['id'],ex);ledger.start(ex['limits']);ledger.claim('trial','execution','hash')
        recover(self.f.store)
        self.assertEqual(self.f.store.get('evo_campaign',p['id'])['status'],'interrupted')
        self.assertEqual(ExperimentBudget(self.f.store,p['id']).public()['unknown_usage_calls'],1)
        self.assertIn('多轮计划',control.resume_reasons(self.f.store,self.f.store.get('evo_experiment',ex['id']))[0])

if __name__=='__main__':unittest.main()
