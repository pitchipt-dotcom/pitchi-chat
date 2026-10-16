"""Finite conversation intent rules. No execution authority or inferred cause."""
import re


def intent(message):
    text = re.sub(r'[\s。？！!?]+', '', message)
    text = re.sub(r'^(じゃあ|では|それなら)', '', text)
    if re.fullmatch(r'(検索改善の)?(変更差分とテスト結果|検索改善の結果|Before/After)(を)?(見せて|教えて)(ください)?', text):
        return 'explain'
    if re.fullmatch(r'(なぜ失敗した(の)?(か教えて(ください)?)?|なぜ失敗したの|何がダメだった|さっきのエラーは|詳しく教えて(ください)?|どうして完成しなかった|なぜ|どうして)', text):
        return 'explain'
    if re.fullmatch(r'(何を追加すればできる(ようになる)?|Codexに直させたい|直すために必要な機能を教えて)', text):
        return 'advice'
    if re.search(r'(削除|消して|上書き|両方|どちら|全部)', text):
        return 'confirm'
    if re.fullmatch(r'(この問題を|このアプリを|これを|さっきのアプリを)?(直して|修正して|直せるようにして)(ください)?', text):
        return 'repair'
    if re.match(r'(これ|このアプリ|さっきのアプリ)', text) and re.search(r'(変更|追加|機能を足)', text):
        return 'modify'
    return 'new'
