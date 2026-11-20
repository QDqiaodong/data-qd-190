from .cards import card
from .checks import array, fields, ident, integer, unique

def validate(data):
    fields(data,('board','players','button_seat'))
    integer(data['button_seat'],0,8)
    cards=[card(c) for c in array(data['board'],5,5)]
    players=array(data['players'],2,9)
    for player in players:
        fields(player,('id','seat','committed','folded','hole'))
        ident(player['id']);integer(player['seat'],0,8);integer(player['committed'],0,1000000000)
        if type(player['folded']) is not bool:raise ValueError('folded must be boolean')
        hole=array(player['hole'],0 if player['folded'] else 2,2)
        if len(hole) not in (0,2):raise ValueError('folded hole must be absent or two cards')
        cards.extend(card(c) for c in hole)
    unique(cards);unique([p['id'] for p in players]);unique([p['seat'] for p in players])
    if not any(not p['folded'] for p in players):raise ValueError('at least one live player required')
    if not sum(p['committed'] for p in players):raise ValueError('positive contribution required')
    return data
