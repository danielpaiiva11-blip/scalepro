"""
ScalePro — Fonte Única de Verdade do cálculo operacional.

Todo módulo (Dashboard, Radar, Análise Operacional, Geração, Escala) deve usar
estas funções. Nenhum KPI é inventado: todos derivam do banco através dos
mesmos caminhos.

Fluxo lógico (igual em todas as telas):
  demanda final (por setor × dia × faixa)
    = demanda_base(daixa) × multiplicador_do_dia × (1 + impacto_feriado/evento)
  cobertura por faixa = escalados_por_faixa / necessarios × 100
  déficit  = max(necessario - escalado, 0)
  excesso  = max(escalado - necessario, 0)
  aderência = 100 - penalidade(deficit + excesso)
  Scale Score = cobertura(35) + aderência(25) + equidade(15) + conflitos(10)
                + jornadas(10) + déficit_crítico(5)
"""

from datetime import date, datetime
from statistics import pstdev

FAIXAS = ["06-08", "08-10", "10-12", "12-14", "14-16", "16-18", "18-20", "20-22"]

# Multiplicador por dia da semana (0=seg .. 6=dom)
MULTIPLICADOR_DIA = [0.92, 0.90, 0.94, 0.98, 1.12, 1.25, 1.08]

# Classificação de risco de uma faixa/setor
def status_risco(cobertura):
    """Cobertura % -> nível de risco do radar."""
    if cobertura is None:
        return "sem"
    if 95 <= cobertura <= 110:
        return "ok"
    if 85 <= cobertura < 95:
        return "atencao"
    if 70 <= cobertura < 85:
        return "risco"
    return "critico"  # < 70 ou > 110 (muito excesso também é sinalizado)


def classificacao(score):
    if score >= 90:
        return "Excelente"
    if score >= 80:
        return "Muito boa"
    if score >= 70:
        return "Boa"
    if score >= 60:
        return "Atenção"
    if score >= 40:
        return "Crítica"
    return "Muito crítica"


def faixa_de_hora(hora):
    inicio = (hora // 2) * 2
    return f"{inicio:02d}-{inicio+2:02d}"


def faixas_do_turno(entrada, saida, intervalo=None):
    """Lista de faixas EFETIVAMENTE cobertas por um turno (entrada/saida HH:MM),
    descontando as faixas do intervalo de descanso (ex.: '12:00 as 14:00')."""
    if not entrada or not saida:
        return []
    ini = int(entrada.split(":")[0])
    fim = int(saida.split(":")[0])
    horas = list(range(ini, 24)) + list(range(0, fim)) if fim <= ini else list(range(ini, fim))
    faixas = {faixa_de_hora(h) for h in horas}
    if intervalo and "as" in intervalo.lower():
        try:
            partes = intervalo.lower().split("as")
            ini_i = int(partes[0].strip().split(":")[0])
            fim_i = int(partes[1].strip().split(":")[0])
            if fim_i <= ini_i:
                hh = list(range(ini_i, 24)) + list(range(0, fim_i))
            else:
                hh = list(range(ini_i, fim_i))
            for h in hh:
                faixas.discard(faixa_de_hora(h))
        except (ValueError, IndexError):
            pass
    return sorted(faixas)


def _dow_index(dia):
    d = datetime.fromisoformat(dia) if isinstance(dia, str) else dia
    return d.weekday()


def _dias_da_faixa(dia):
    """Dia 1..5 (+10%), 25..30 (+8%), resto sem ajuste."""
    d = date.fromisoformat(dia) if isinstance(dia, str) else dia
    if 1 <= d.day <= 5:
        return 0.10
    if 25 <= d.day <= 30:
        return 0.08
    return 0.0


def eh_feriado(db, dia):
    return db.execute(
        "SELECT id FROM calendario WHERE data=? AND ativo=1", (dia,)).fetchone() is not None


class AnaliseEscala:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ #
    # Demanda (única fonte)                                              #
    # ------------------------------------------------------------------ #
    def demanda_base(self, setor_id, dia=None):
        """demanda_faixa base do setor (dia_semana específico, depois 8=todos)."""
        db = self.db
        if dia is None:
            rows = db.execute(
                "SELECT faixa, necessarios FROM demanda_faixa WHERE setor_id=? AND dia_semana=8",
                (setor_id,)).fetchall()
        else:
            dow = _dow_index(dia)
            rows = db.execute(
                "SELECT faixa, necessarios FROM demanda_faixa WHERE setor_id=? "
                "AND dia_semana IN (?,8) ORDER BY dia_semana DESC",
                (setor_id, dow)).fetchall()
        dem = {}
        for r in rows:
            dem.setdefault(r["faixa"], r["necessarios"])
        return dem

    def demanda_final(self, setor_id, dia):
        """Demanda realista por faixa no dia, aplicando multiplicador do dia
        e impactos de feriado/evento/véspera/início e fim de mês."""
        dem = self.demanda_base(setor_id, dia)
        if not dem:
            return dem
        dow = _dow_index(dia)
        mult = MULTIPLICADOR_DIA[dow]
        # impacto feriado / evento
        imp_extra = 0.0
        db = self.db
        fer = db.execute(
            """SELECT c.id, COALESCE((SELECT fi.impacto FROM feriado_impacto fi
                WHERE fi.calendario_id=c.id AND fi.setor_id=?), 15) imp
               FROM calendario c WHERE c.data=? AND c.ativo=1""",
            (setor_id, dia)).fetchone()
        if fer:
            imp_extra += fer["imp"] / 100.0
        # véspera de feriado: dia seguinte é feriado
        prox = (date.fromisoformat(dia) + __import__("datetime").timedelta(days=1)).isoformat()
        if eh_feriado(db, prox):
            imp_extra += 0.15
        # início/fim de mês
        imp_extra += _dias_da_faixa(dia)
        # promoção/evento configurado por data
        promo = db.execute(
            "SELECT valor FROM config WHERE chave='promocao_' || ?", (dia,)).fetchone()
        if promo:
            try:
                imp_extra += float(promo["valor"]) / 100.0
            except (TypeError, ValueError):
                pass

        final = {}
        for fx, n in dem.items():
            v = n * mult * (1 + imp_extra)
            final[fx] = max(0, int(round(v)))
        return final

    # ------------------------------------------------------------------ #
    # Escalados / disponibilidade                                        #
    # ------------------------------------------------------------------ #
    def escalados_por_faixa(self, setor_id, dia):
        """Funcionários REALMENTE PRESENTES no dia (escalados e sem ausência/folga),
        contados por faixa via turno."""
        db = self.db
        indisp = set()
        indisp |= {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM ausencias WHERE data=?", (dia,)).fetchall()}
        indisp |= {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM bloqueios WHERE data=?", (dia,)).fetchall()}
        rows = db.execute(
            """SELECT f.id, t.entrada, t.saida, t.intervalo FROM escala e
               JOIN funcionarios f ON f.id=e.funcionario_id
               LEFT JOIN funcionario_turno ft ON ft.funcionario_id=f.id
               LEFT JOIN turnos t ON t.id=ft.turno_id
               WHERE f.setor_id=? AND e.data=?""", (setor_id, dia)).fetchall()
        conta = {}
        for r in rows:
            if r["id"] in indisp:
                continue
            for fx in faixas_do_turno(r["entrada"], r["saida"], r["intervalo"]):
                conta[fx] = conta.get(fx, 0) + 1
        return conta

    def disponiveis_por_faixa(self, setor_id, dia):
        """Funcionários DISPONÍVEIS no dia (ativos, sem ausência, sem folga),
        contados por faixa via turno."""
        db = self.db
        indisp = set()
        indisp |= {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM ausencias WHERE data=?", (dia,)).fetchall()}
        indisp |= {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM bloqueios WHERE data=?", (dia,)).fetchall()}
        rows = db.execute(
            """SELECT f.id, t.entrada, t.saida, t.intervalo FROM funcionarios f
               LEFT JOIN funcionario_turno ft ON ft.funcionario_id=f.id
               LEFT JOIN turnos t ON t.id=ft.turno_id
               WHERE f.ativo=1 AND f.setor_id=?""", (setor_id,)).fetchall()
        conta = {}
        for r in rows:
            if r["id"] in indisp:
                continue
            for fx in faixas_do_turno(r["entrada"], r["saida"], r["intervalo"]):
                conta[fx] = conta.get(fx, 0) + 1
        return conta

    # ------------------------------------------------------------------ #
    # Diagnóstico por dia (todas as telas usam isto)                     #
    # ------------------------------------------------------------------ #
    def analise_dia(self, dia, setor_id=None):
        """Cálculo unificado de cobertura/déficit/excesso por setor e por faixa."""
        db = self.db
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
        if setor_id:
            setores = [s for s in setores if s["id"] == setor_id]

        resumo_por_setor = []
        faixa_total = {}
        total_deficit = total_excesso = total_demanda = total_escalados = 0

        for s in setores:
            dem = self.demanda_final(s["id"], dia)
            esc = self.escalados_por_faixa(s["id"], dia)
            faixas = sorted(set(dem) | set(esc))
            deficit_s = excesso_s = 0
            coberturas = []
            for fx in faixas:
                nec = dem.get(fx, 0)
                disp = esc.get(fx, 0)
                total_demanda += nec
                total_escalados += disp
                if nec:
                    coberturas.append(round(100 * disp / nec))
                if disp < nec:
                    deficit_s += (nec - disp)
                else:
                    excesso_s += (disp - nec)
                t = faixa_total.setdefault(fx, {"nec": 0, "esc": 0, "def": 0, "exc": 0})
                t["nec"] += nec
                t["esc"] += disp
                t["def"] += max(0, nec - disp)
                t["exc"] += max(0, disp - nec)
            total_deficit += deficit_s
            total_excesso += excesso_s
            cob_media = round(sum(coberturas) / len(coberturas)) if coberturas else None
            resumo_por_setor.append({
                "setor": s["nome"], "setor_id": s["id"],
                "cobertura": cob_media, "deficit": deficit_s, "excesso": excesso_s,
                "demanda": sum(dem.values()), "escalados": sum(esc.values()),
            })

        # faixas agregadas (todas as telas mostram o MESMO número)
        faixas_resumo = []
        for fx in FAIXAS:
            t = faixa_total.get(fx, {"nec": 0, "esc": 0, "def": 0, "exc": 0})
            cob = round(100 * t["esc"] / t["nec"]) if t["nec"] else None
            faixas_resumo.append({
                "faixa": fx, "necessarios": t["nec"], "escalados": t["esc"],
                "deficit": t["def"], "excesso": t["exc"],
                "cobertura": cob, "status": status_risco(cob),
            })

        cobertura_geral = round(100 * total_escalados / total_demanda) if total_demanda else None
        pior_cob = min([f["cobertura"] for f in faixas_resumo if f["cobertura"] is not None] or [cobertura_geral])
        pior_setor = min([s for s in resumo_por_setor if s["cobertura"] is not None],
                         key=lambda x: x["cobertura"])["setor"] if [s for s in resumo_por_setor if s["cobertura"] is not None] else None

        return {
            "dia": dia,
            "setores": resumo_por_setor,
            "faixas": faixas_resumo,
            "total_deficit": total_deficit,
            "total_excesso": total_excesso,
            "total_demanda": total_demanda,
            "total_escalados": total_escalados,
            "cobertura_geral": cobertura_geral,
            "cobertura_pico": pior_cob,
            "pior_setor": pior_setor,
        }

    # ------------------------------------------------------------------ #
    # Escala score + aderência + equidade                                #
    # ------------------------------------------------------------------ #
    def equidade(self, setor_id=None):
        """Índice de equidade (0-100) baseado no coeficiente de variação das
        escalas por funcionário ativo. Menor concentração = maior nota."""
        db = self.db
        q = """SELECT f.id, f.setor_id, COUNT(e.id) total FROM funcionarios f
               LEFT JOIN escala e ON e.funcionario_id=f.id
               WHERE f.ativo=1"""
        p = []
        if setor_id:
            q += " AND f.setor_id=?"
            p.append(setor_id)
        q += " GROUP BY f.id"
        vals = [r["total"] for r in db.execute(q, p).fetchall()]
        if not vals:
            return 100
        media = sum(vals) / len(vals)
        if media == 0:
            return 100
        cv = pstdev(vals) / media if len(vals) > 1 else 0
        return max(0, round(100 * (1 - min(cv, 1.5) / 1.5)))

    def _aderencia(self, an):
        """Aderência (0-100): penaliza déficit e excesso juntos.
        Ideal próximo de 100 sem déficit nem desperdício."""
        dem = an["total_demanda"]
        if dem == 0:
            return 100
        penal = an["total_deficit"] * 2 + an["total_excesso"] * 1.2
        return max(0, round(100 * max(0, 1 - penal / dem)))

    def scale_score(self, an, setor_id=None):
        """Nota 0-100 com composição fixa e reagindo aos dados reais."""
        db = self.db
        # 1) cobertura adequada (35) — quanto mais perto de 100% melhor
        cob = an["cobertura_geral"]
        if cob is None:
            cob_p = 0
        else:
            cob_p = 35 * max(0, 1 - abs(cob - 100) / 60)
        # 2) aderência (25)
        ader = self._aderencia(an)
        ader_p = 25 * ader / 100
        # 3) equidade (15)
        eq_p = 15 * self.equidade(setor_id) / 100
        # 4) ausência de conflitos (10) — sem dupla alocação no dia
        conflitos = db.execute(
            """SELECT data, COUNT(DISTINCT data) - COUNT(*) FROM (
                 SELECT data, funcionario_id, COUNT(*) n FROM escala
                 GROUP BY data, funcionario_id HAVING n>1)""").fetchone()
        conf_p = 0 if (conflitos and conflitos[1]) else 10
        # 5) distribuição de jornadas (10) — cobertura entre 90 e 110
        jornadas = sum(1 for f in an["faixas"] if f["cobertura"] is not None and 90 <= f["cobertura"] <= 110)
        faixas_com_demanda = sum(1 for f in an["faixas"] if f["necessarios"] > 0)
        jor_p = 10 * (jornadas / faixas_com_demanda) if faixas_com_demanda else 10
        # 6) déficit crítico (5) — +5 se não há déficit crítico
        criticos = sum(1 for f in an["faixas"] if f["status"] == "critico")
        crit_p = 0 if criticos else 5

        score = max(0, min(100, round(cob_p + ader_p + eq_p + conf_p + jor_p + crit_p)))
        return {
            "score": score,
            "classificacao": classificacao(score),
            "breakdown": {
                "cobertura": round(cob_p, 1),
                "aderencia": round(ader_p, 1),
                "equidade": round(eq_p, 1),
                "conflitos": conf_p,
                "jornadas": round(jor_p, 1),
                "deficit_critico": crit_p,
            },
            "equidade": self.equidade(setor_id),
            "aderencia_pct": ader,
            "conflitos": bool(conflitos and conflitos[1]),
        }

    def diagnostico(self, dia, setor_id=None):
        """API compatível usada por radar/dashboard, enriquecida."""
        an = self.analise_dia(dia, setor_id)
        score = self.scale_score(an, setor_id)
        resumo_por_setor = an["setores"]
        for s in resumo_por_setor:
            s["cobertura"] = s["cobertura"]
        return {
            "dia": dia,
            "cobertura_geral": an["cobertura_geral"],
            "cobertura_pico": an["cobertura_pico"],
            "aderencia": score["aderencia_pct"],
            "total_deficit": an["total_deficit"],
            "total_excesso": an["total_excesso"],
            "setores": resumo_por_setor,
            "faixas": an["faixas"],
            "pior_setor": an["pior_setor"],
            "score": score["score"],
            "classificacao": score["classificacao"],
            "score_breakdown": score["breakdown"],
            "equidade": score["equidade"],
            "conflitos": score["conflitos"],
            "pontos": pontos_feedback(resumo_por_setor, score["score"], an["faixas"]),
        }

    # ------------------------------------------------------------------ #
    # Teste de Resiliência (corrigido)                                   #
    # ------------------------------------------------------------------ #
    def resiliencia(self, dia, setor_id=None):
        """Simula cenários de estresse recalculando a cobertura real.
        Baseado na cobertura do pior horário (pico) — mais honesto que a média,
        pois a média é inflada pelo excesso do meio do dia."""
        an = self.analise_dia(dia, setor_id)
        base_cob = an["cobertura_geral"] or 0
        dem = an["total_demanda"]
        esc = an["total_escalados"]
        robustez = float(base_cob)

        cenarios = []
        def _add(titulo, cob, def_, sev):
            cenarios.append({"cenario": titulo, "cobertura": cob,
                             "deficit_criado": def_, "status": status_risco(cob), "gravidade": sev})

        for n in (1, 2):
            cob = round(100 * max(0, esc - n) / dem) if dem else None
            _add(f"Ausência de {n} funcionário(s)", cob, max(0, an["total_deficit"] + n),
                 "leve" if n == 1 else "moderado")
            if cob is not None and cob < 70:
                robustez -= 8
        for pct in (5, 10):
            n = max(1, int(esc * pct / 100))
            cob = round(100 * max(0, esc - n) / dem) if dem else None
            _add(f"Ausência de {pct}%", cob, max(0, an["total_deficit"] + n),
                 "moderado" if pct == 5 else "grave")
            if cob is not None and cob < 70:
                robustez -= 10
        for pct in (10, 20):
            cob = round(100 * esc / (dem * (1 + pct / 100))) if dem else None
            _add(f"Demanda +{pct}%", cob, max(0, int(dem * (1 + pct / 100) - esc)),
                 "moderado" if pct == 10 else "grave")
            if cob is not None and cob < 70:
                robustez -= 8

        vulneraveis = [s["setor"] for s in an["setores"] if (s["cobertura"] or 100) < 85]
        robustez = max(0, min(100, round(robustez)))
        return {
            "dia": dia,
            "robustez": robustez,
            "cobertura_base": base_cob,
            "cenarios": cenarios,
            "setores_vulneraveis": vulneraveis,
            "deficit_base": an["total_deficit"],
            "pontos": pontos_feedback(an["setores"], robustez, an["faixas"]),
        }

    # ------------------------------------------------------------------ #
    # Heatmap (setor × faixa)                                            #
    # ------------------------------------------------------------------ #
    def heatmap(self, dia, setor_id=None):
        db = self.db
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
        if setor_id:
            setores = [s for s in setores if s["id"] == setor_id]
        matriz = []
        for s in setores:
            dem = self.demanda_final(s["id"], dia)
            esc = self.escalados_por_faixa(s["id"], dia)
            linha = {"setor": s["nome"]}
            for fx in FAIXAS:
                nec = dem.get(fx, 0)
                disp = esc.get(fx, 0)
                linha[fx] = round(100 * disp / nec) if nec else None
            matriz.append(linha)
        return {"faixas": FAIXAS, "matriz": matriz}

    # ------------------------------------------------------------------ #
    # Remanejamento                                                      #
    # ------------------------------------------------------------------ #
    def remanejamento(self, dia, setor_id=None):
        db = self.db
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
        if setor_id:
            setores = [s for s in setores if s["id"] == setor_id]
        info = []
        for s in setores:
            dem = self.demanda_final(s["id"], dia)
            esc = self.escalados_por_faixa(s["id"], dia)
            nec_total = sum(dem.values())
            esc_total = sum(esc.values())
            saldo = esc_total - nec_total
            cob = round(100 * esc_total / nec_total) if nec_total else None
            info.append({
                "setor": s["nome"], "setor_id": s["id"],
                "necessario": nec_total, "escalado": esc_total,
                "saldo": saldo, "cobertura": cob,
            })
        com_deficit = [i for i in info if i["saldo"] < 0]
        com_excesso = [i for i in info if i["saldo"] > 0]
        sugestoes = []
        for ex in com_excesso:
            for de in com_deficit:
                cands = db.execute(
                    """SELECT f.nome, f.id FROM funcionarios f
                       WHERE f.setor_id=? AND f.ativo=1
                       AND (f.id IN (SELECT funcionario_id FROM funcionario_habilidade WHERE setor_id=?))
                       LIMIT 3""", (ex["setor_id"], de["setor_id"])).fetchall()
                for c in cands:
                    sugestoes.append({
                        "funcionario": c["nome"], "de": ex["setor"], "para": de["setor"],
                        "impacto_de": ex["cobertura"], "impacto_para": de["cobertura"],
                    })
        return {"setores": info, "sugestoes": sugestoes[:8]}


def pontos_feedback(setores, score, faixas=None):
    pos, neg = [], []
    for s in setores:
        if s["cobertura"] is None:
            continue
        if 95 <= s["cobertura"] <= 110 and s["deficit"] == 0:
            pos.append(f"{s['setor']} com cobertura adequada ({s['cobertura']}%)")
        elif s["cobertura"] > 110:
            neg.append(f"{s['setor']} com excesso de efetivo ({s['cobertura']}%)")
        elif s["cobertura"] < 85:
            neg.append(f"{s['setor']} abaixo de 85% ({s['cobertura']}%)")
        if s["deficit"] > 0:
            neg.append(f"{s['setor']} com déficit de {s['deficit']} pessoa(s)")
    if faixas:
        for f in faixas:
            if f["status"] == "critico" and f["necessarios"] > 0:
                neg.append(f"faixa {f['faixa']} crítica ({f['cobertura']}%)")
            elif f["cobertura"] is not None and f["cobertura"] > 110:
                neg.append(f"faixa {f['faixa']} com desperdício ({f['cobertura']}%)")
    return {"positivos": pos[:4], "negativos": neg[:4]}