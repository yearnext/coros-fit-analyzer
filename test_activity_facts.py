import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from activity_facts import analyze


def rows(n=41, seconds=10, dz=1, dd=20):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [dict(timestamp=(start+timedelta(seconds=i*seconds)).isoformat(), distance=i*dd,
                 altitude=100+i*dz, latitude=30+i*0.00018, longitude=120,
                 heart_rate=140, cadence=80, speed=dd/seconds) for i in range(n)]


class FactsTests(unittest.TestCase):
    def test_empty(self):
        f=analyze([])
        self.assertEqual(f['status'],'insufficient_data')
        self.assertIsNone(f['overview']['distance_m']['value'])
        self.assertEqual(f['terrain_segments']['status'],'blocked')

    def test_no_fabrication(self):
        f=analyze([{'timestamp':'2026-01-01T00:00:00Z'}, {'timestamp':'2026-01-01T00:00:01Z'}])
        for field in ('heart_rate','cadence','step_length','stance_time'):
            self.assertIsNone(f['overview']['fields'][field]['mean']['value'])
        self.assertEqual(f['running_dynamics']['availability']['step_length']['status'],'not_recorded')
        self.assertEqual(f['nutrition']['status'],'blocked')
        self.assertEqual(f['compare_activities']['status'],'blocked')

    def test_climb_and_vam(self):
        f=analyze(rows())
        s=f['terrain_segments']['natural_segments'][0]
        self.assertEqual(s['kind'],'climb')
        self.assertAlmostEqual(s['vam_m_h'],s['net_elevation_m']/400*3600)
        self.assertEqual(s['net_elevation_m'],39)  # 1 m residual below the 3 m anchor threshold
        self.assertAlmostEqual(s['pace_seconds_per_km']['value'],500)
        self.assertEqual(s['fields']['heart_rate']['mean']['value'],140)
        self.assertIsNotNone(s['horizontal_grade_percent'])
        self.assertEqual(s['evidence_grade'],'B')

    def test_hysteresis_plateaus_do_not_split(self):
        f=analyze(rows(n=61, dz=0.5))
        self.assertEqual(len(f['terrain_segments']['natural_segments']),1)
        self.assertEqual(f['terrain_segments']['total_ascent_m'],30)

    def test_descent_not_uphill_vam(self):
        f=analyze(rows(dz=-1))
        self.assertIsNone(f['terrain_segments']['natural_segments'][0]['vam_m_h'])

    def test_gap_never_bridged(self):
        r=rows()
        for i in range(20,len(r)):
            r[i]['timestamp']=(datetime.fromisoformat(r[i]['timestamp'])+timedelta(seconds=300)).isoformat()
        f=analyze(r)
        self.assertEqual(len(f['terrain_segments']['natural_segments']),2)
        self.assertEqual(f['overview']['distance_m']['value'],780)
        self.assertTrue(any(a['kind']=='timestamp_gap' for a in f['quality_report']['anomalies']))

    def test_missing_altitude_splits(self):
        r=rows(); del r[20]['altitude']
        f=analyze(r)
        self.assertEqual(len(f['terrain_segments']['natural_segments']),2)

    def test_spikes_excluded(self):
        r=rows(seconds=1,dd=2); r[20]['heart_rate']=0; r[21]['altitude']=1000; r[22]['latitude']=40
        f=analyze(r)
        self.assertEqual(f['overview']['fields']['heart_rate']['mean']['value'],140)
        self.assertLess(f['terrain_segments']['total_ascent_m'],100)
        kinds={a['kind'] for a in f['quality_report']['anomalies']}
        self.assertIn('gps_jump',kinds); self.assertIn('altitude_jump',kinds)

    def test_duplicate_and_backwards(self):
        r=rows(); r[10]['timestamp']=r[9]['timestamp']; r[20]['timestamp']=r[18]['timestamp']
        f=analyze(r)
        self.assertIsNone(f['overview']['elapsed_seconds'])
        kinds={a['kind'] for a in f['quality_report']['anomalies']}
        self.assertIn('duplicate_timestamp',kinds); self.assertIn('backwards_timestamp',kinds)

    def test_pause_events(self):
        r=rows()
        events={'event':[{'event':'timer','event_type':'stop_all','timestamp':r[10]['timestamp']},
                         {'event':'timer','event_type':'start','timestamp':r[20]['timestamp']}]}
        f=analyze(r,events)
        self.assertEqual(f['overview']['timer_pause_seconds'],100)
        self.assertEqual(f['overview']['active_seconds']['value'],300)
        self.assertEqual(f['overview']['distance_m']['value'],600)

    def test_hr_zones_require_personal_input(self):
        self.assertEqual(analyze(rows())['hr_dynamics']['zones']['status'],'blocked')
        with self.assertRaises(ValueError): analyze(rows(),hr_zone_boundaries=[100,160],hr_zone_source='age')
        with self.assertRaises(ValueError): analyze(rows(),hr_zone_boundaries=[160,100],hr_zone_source='user_explicit')
        f=analyze(rows(),hr_zone_boundaries=[100,140,160],hr_zone_source='user_explicit')
        self.assertEqual(f['hr_dynamics']['zones']['seconds'],[0,400])

    def test_gate(self):
        for pain in ('unknown','unresolved'):
            gate=analyze(rows(),pain_status=pain,requested_stage='D')['training_gate']
            self.assertFalse(gate['requested_stage_allowed'])
            self.assertIn('heavy_downhill',gate['prohibited'])
        self.assertIsNone(analyze(rows(),pain_status='resolved',requested_stage='D')['training_gate']['current_stage'])

    def test_nutrition_missing_not_zero(self):
        r=rows()
        f=analyze(r,nutrition_log=[{'timestamp':r[10]['timestamp'],'carbohydrate_g':30}])
        self.assertAlmostEqual(f['nutrition']['carbohydrate_g_per_hour'],270)
        self.assertIsNone(f['nutrition']['water_ml_per_hour'])
        with self.assertRaises(ValueError): analyze(r,nutrition_log=[{'timestamp':'2020-01-01'}])

    def test_weather_failure_and_sampling(self):
        def fail(*args): raise ValueError('API unavailable')
        f=analyze(rows(),weather_loader=fail)
        self.assertEqual(f['environment']['weather']['status'],'unverified')
        self.assertTrue(all(s['weather'] is None for s in f['environment']['weather_samples']))
        self.assertLessEqual(len(f['environment']['weather_samples']),6)

    def test_overlap_gate(self):
        r=rows()
        f=analyze(r,previous_records=r)
        self.assertEqual(f['compare_activities']['overlap_sample_fraction'],1)
        self.assertFalse(f['compare_activities']['performance_conclusion_allowed'])
        p=rows()
        for row in p: row['latitude']+=1
        self.assertEqual(analyze(r,previous_records=p)['compare_activities']['overlap_sample_fraction'],0)

    def test_no_mutation_and_strict_json(self):
        r=rows(); before=copy.deepcopy(r)
        json.dumps(analyze(r),allow_nan=False)
        self.assertEqual(r,before)

    def test_distance_reset(self):
        r=rows(); r[20]['distance']=0
        f=analyze(r)
        self.assertTrue(any(a['kind']=='distance_reset_or_jump' for a in f['quality_report']['anomalies']))

    def test_splits_do_not_interpolate(self):
        f=analyze(rows(n=121))
        splits=f['terrain_segments']['distance_splits_m']['1000']
        self.assertEqual(len(splits),3)
        self.assertAlmostEqual(sum(s['distance_m']['value'] for s in splits),2400)


if __name__=='__main__': unittest.main()
