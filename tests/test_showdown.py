import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from showdown_pots.cards import rank_five, best_seven
from showdown_pots.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class ShowdownTests(unittest.TestCase):
    def test_side_pot_eligibility_and_uncalled_return(self):
        r=process(sample())
        self.assertEqual(r['summary'],dict(players=4,pots=3,total_committed=240,total_awarded=210,total_returned=30,net_sum=0))
        self.assertEqual([p['amount'] for p in r['pots']],[80,90,40])
        self.assertEqual([p['winner_ids_in_odd_chip_order'] for p in r['pots']],[['straight'],['ace'],['ace']])
        self.assertEqual([p['net'] for p in r['players']],[60,-50,60,-70])

    def test_category_order_and_wheel(self):
        hands=['2c 5d 7h 9s Jc','2c 2d 7h 9s Jc','2c 2d 7h 7s Jc','2c 2d 2h 9s Jc','Ac 2d 3h 4s 5c','2c 5c 7c 9c Jc','2c 2d 2h 9s 9c','2c 2d 2h 2s Jc','2c 3c 4c 5c 6c']
        ranks=[rank_five(h.split()) for h in hands]
        self.assertEqual([r[0] for r in ranks],list(range(9)))
        self.assertEqual(ranks[4],(4,5))
        self.assertEqual(sorted(ranks),ranks)
        self.assertEqual(rank_five('Qc Kd Ah 2s 3c'.split())[0],0)

    def test_best_seven_two_triples_and_input_order(self):
        cards='Ah As Ad Kh Ks Kd 2c'.split()
        h=best_seven(cards)
        self.assertEqual(h['rank'],[6,14,13])
        self.assertEqual(h,best_seven(cards[::-1]))

    def test_tied_board_odd_chip_clockwise_after_button(self):
        d=sample();d['board']='Tc Jc Qc Kc Ac'.split();d['button_seat']=1
        d['players']=d['players'][:3]
        for p in d['players']:p['committed']=1
        d['players'][1].update(folded=True,hole=[])
        r=process(d)
        self.assertEqual(r['pots'][0]['winner_ids_in_odd_chip_order'],['straight','ace'])
        self.assertEqual(r['pots'][0]['awards'],{'straight':2,'ace':1})

    def test_single_eligible_wins_folded_money(self):
        d=sample()
        for p in d['players'][1:]:p.update(folded=True,hole=[])
        r=process(d)
        self.assertEqual(r['players'][0]['net'],140)
        self.assertEqual(sum(p['net'] for p in r['players']),0)

    def test_invalid_duplicate_cards_and_dead_layer(self):
        d=sample();d['players'][0]['hole'][0]='2c'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['players'][1]['seat']=0
        with self.assertRaises(ValueError):process(d)
        d=sample();d['players'][0]['committed']=1;d['players'][1]['committed']=1;d['players'][2]['committed']=1
        d['players'].append(dict(id='fold2',seat=4,committed=70,folded=True,hole=[]))
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
