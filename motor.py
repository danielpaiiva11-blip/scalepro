"""
ScalePro — Motor de Otimização de Escala (Workforce Planning).

Prioridade das regras (em ordem):
  1. restrições obrigatórias (bloqueios: férias/atestado/folga)
  2. funcionário ativo
  3. função/setor habilitado (inclui polivalência)
  4. disponibilidade (turno/horário)
  5. cobertura mínima do setor
  6. horários de pico
  7. demanda por faixa horária
  8. menor déficit
  9. menor excesso
 10. menor hora extra
 11. equilíbrio do rodízio
 12. distribuição justa de domingos e feriados

Modos de geração (ver MODOS). Sorteio é usado apenas como último desempate,
nunca como estratégia principal.
"""

import random
from datetime import date, datetime, timedelta

# Semana: 0=segunda ... 6=domingo
DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}

# Faixas horárias padrão (configuráveis; aqui descrevemos o formato 2h)
FAIXAS_PADRAO = ["06-08", "08-10", "10-12", "12-14", "14-16", "16-18", "18-20", "20-22"]

STATUS_ESCALA = ["rascunho", "simulada", "validada", "aprovada", "publicada", "encerrada"]

# 10 modos de geração + descrição para o painel explicativo
MODOS = {
    "equilibrada": {
        "nome": "⚖ Escala Equilibrada",
        "objetivo": "Equilibrar cobertura, demanda, rodízio, domingos, feriados, abertura, fechamento e quantidade de escalas.",
        "prioriza": ["cobertura", "demanda", "rodízio", "domingos/feriados", "abertura/fechamento", "quantidade de escalas"],
        "ideal": "rotina normal da loja, sem eventos especiais.",
        "menor_prioridade": "otimizações específicas de um único fator.",
    },
    "por_pico": {
        "nome": "🎯 Escala por Horário de Pico",
        "objetivo": "Garantir mais funcionários nos períodos de maior demanda.",
        "prioriza": ["horários críticos", "demanda por setor", "cobertura mínima", "disponíveis", "polivalentes"],
        "ideal": "sexta/sábado, feriados, promoções, pagamento, horários de grande fluxo.",
        "menor_prioridade": "distribuição uniforme em horários tranquilos.",
    },
    "maxima_cobertura": {
        "nome": "🛡 Máxima Cobertura",
        "objetivo": "Eliminar déficits e manter todos os setores cobertos.",
        "prioriza": ["eliminar déficit", "setores cobertos", "cobertura mínima"],
        "ideal": "dias críticos ou quando falta pessoal.",
        "menor_prioridade": "economia de horas extras e rodízio perfeito.",
    },
    "rodizio_justo": {
        "nome": "🔄 Rodízio Justo",
        "objetivo": "Distribuir de forma equilibrada domingos, feriados, aberturas, fechamentos e escalas acumuladas.",
        "prioriza": ["domingos", "feriados", "abertura", "fechamento", "escalas acumuladas"],
        "ideal": "longo prazo, evitando sobrecarga de uns sobre outros.",
        "menor_prioridade": "cobertura imediata de picos pontuais.",
    },
    "reducao_horas_extras": {
        "nome": "⏱ Redução de Horas Extras",
        "objetivo": "Atender a demanda usando primeiro a jornada normal disponível.",
        "prioriza": ["jornada normal", "menor hora extra", "menor custo adicional"],
        "ideal": "contenção de custos com folha.",
        "menor_prioridade": "flexibilidade em horários incomuns.",
    },
    "feriado": {
        "nome": "🎉 Escala de Feriado",
        "objetivo": "Gerar escala específica para um ou mais feriados escolhidos.",
        "prioriza": ["feriado selecionado", "demanda do feriado", "rodízio de feriados"],
        "ideal": "datas comemorativas e feriados oficiais.",
        "menor_prioridade": "dias comuns do calendário.",
    },
    "fim_semana": {
        "nome": "📅 Escala de Fim de Semana",
        "objetivo": "Gerar escalas de sábado e domingo considerando demanda e rodízio.",
        "prioriza": ["sábado/domingo", "demanda do fds", "rodízio de fins de semana"],
        "ideal": "planejamento semanal do fim de semana.",
        "menor_prioridade": "dias úteis.",
    },
    "emergencial": {
        "nome": "🚨 Escala Emergencial",
        "objetivo": "Reorganizar somente os períodos afetados por falta, atraso, afastamento ou demanda inesperada.",
        "prioriza": ["períodos afetados", "reposição rápida", "remanejamento"],
        "ideal": "imprevistos do dia-a-dia operacional.",
        "menor_prioridade": "regenerar a escala inteira.",
    },
    "evento": {
        "nome": "🛒 Escala para Evento/Promoção",
        "objetivo": "Criar reforço temporário baseado no aumento esperado da demanda.",
        "prioriza": ["aumento de demanda", "reforço nos picos", "setores do evento"],
        "ideal": "promoções, datas comerciais e eventos da loja.",
        "menor_prioridade": "rotina normal.",
    },
    "personalizada": {
        "nome": "⚙ Personalizada",
        "objetivo": "Permitir configurar pesos manualmente para cada fator.",
        "prioriza": ["pesos configurados pelo gestor"],
        "ideal": "ajuste fino por especialista.",
        "menor_prioridade": "regras automáticas fixas.",
    },
}

ESTRATEGIAS = {k: v["nome"] for k, v in MODOS.items()}


def _iso(d):
    return d if isinstance(d, str) else d.isoformat()


def _dow(data):
    """Retorna nome curto do dia da semana (seg..dom)."""
    if isinstance(data, str):
        data = datetime.fromisoformat(data)
    return ["seg", "ter", "qua", "qui", "sex", "sab", "dom"][data.weekday()]


def _semana(data):
    """Retorna 0=seg .. 6=dom."""
    if isinstance(data, str):
        data = datetime.fromisoformat(data)
    return data.weekday()


class MotorEscala:
    def __init__(self, db):
        self.db = db
        self.rng = random.Random()

    # ---------- métricas do candidato ----------
    def _metrica(self, func_id):
        db = self.db
        total = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE funcionario_id=?", (func_id,)).fetchone()["c"]
        horas_extras = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE funcionario_id=? AND tipo IN ('fds','feriado')",
            (func_id,)).fetchone()["c"]
        domingos = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE funcionario_id=? AND strftime('%w', data)='0'",
            (func_id,)).fetchone()["c"]
        feriados_n = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE funcionario_id=? AND tipo='feriado'",
            (func_id,)).fetchone()["c"]
        ultima = db.execute(
            "SELECT MAX(data) d FROM escala WHERE funcionario_id=?", (func_id,)).fetchone()["d"]
        return {
            "total": total,
            "horas_extras": horas_extras,
            "domingos": domingos,
            "feriados": feriados_n,
            "ultima": ultima,
        }

    def _candidatos(self, setor_id, dia, tipo):
        """Funcionários elegíveis para um setor em um dia."""
        db = self.db
        if setor_id:
            q = """SELECT f.id, f.nome, f.carga_horas, ft.turno_id
                   FROM funcionarios f
                   LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
                   WHERE f.ativo = 1 AND (
                        f.setor_id = ?
                        OR f.id IN (SELECT funcionario_id FROM funcionario_habilidade WHERE setor_id = ?)
                   )"""
            cands = db.execute(q, (setor_id, setor_id)).fetchall()
        else:
            cands = db.execute(
                """SELECT f.id, f.nome, f.carga_horas, ft.turno_id
                   FROM funcionarios f
                   LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
                   WHERE f.ativo = 1""").fetchall()

        bloq = {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM bloqueios WHERE data = ?", (_iso(dia),)).fetchall()}
        aus = {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM ausencias WHERE data = ?", (_iso(dia),)).fetchall()}

        elegiveis = []
        for f in cands:
            if f["id"] in bloq or f["id"] in aus:
                continue
            m = self._metrica(f["id"])
            elegiveis.append({**dict(f), **m})
        return elegiveis

    def _escore(self, c, peso, modo, demandas=None, dia=None):
        """Score do candidato. Menor = maior prioridade."""
        s = 0.0
        if modo == "maxima_cobertura":
            s = c["total"] * 10 + c["horas_extras"] * 2
        elif modo == "reducao_horas_extras":
            s = c["horas_extras"] * 100 + c["total"] * 5 + c["feriados"] * 3
        elif modo == "rodizio_justo":
            s = c["domingos"] * 100 + c["feriados"] * 120 + c["total"] * 8
        elif modo == "por_pico":
            # tapa primeiro os déficits críticos: quem tem menos escala é mobilizável
            s = c["total"] * 15 + c["horas_extras"] * 3
        elif modo == "personalizada":
            s = (c["total"] * peso.get("total", 1)
                 + c["horas_extras"] * peso.get("horas_extras", 1)
                 + c["domingos"] * peso.get("domingos", 1)
                 + c["feriados"] * peso.get("feriados", 1))
        else:  # equilibrada / feriado / fim_semana / emergencial / evento
            s = c["total"] * 12 + c["horas_extras"] * 6 + c["domingos"] * 30 + c["feriados"] * 40
        s += (0 if c["ultima"] else 50)
        s += self.rng.random() * 0.5
        return s

    def otimizar(self, datas_dias, qtd_por_dia, tipo, setor_id,
                 modo="equilibrada", peso=None, seed=None):
        """Gera alocações otimizadas (modo por quantidade fixa de pessoas/dia).
        Retorna (alocacoes, total)."""
        if seed is not None:
            self.rng.seed(seed)
        peso = peso or {}
        alocacoes = []
        total = 0
        for dia in datas_dias:
            cands = self._candidatos(setor_id, dia, tipo)
            if not cands:
                continue
            # evita dupla alocação dentro do mesmo dia
            vistos = set()
            cands.sort(key=lambda c: self._escore(c, peso, modo, None, dia))
            for c in cands[:qtd_por_dia]:
                if c["id"] in vistos:
                    continue
                vistos.add(c["id"])
                alocacoes.append({
                    "data": _iso(dia), "tipo": tipo,
                    "funcionario_id": c["id"], "nome": c["nome"], "status": "rascunho",
                })
                total += 1
        return alocacoes, total

    # ---------- geração por horário de pico ----------
    def _demanda_por_hora(self, setor_id, dia):
        """Retorna dict hora->necessarios para um setor em um dia.
        Usa demanda_hora_setor (dia_semana específico, depois 8=todos)."""
        db = self.db
        dow = _semana(dia)
        rows = db.execute(
            """SELECT hora, necessarios FROM demanda_hora_setor
               WHERE setor_id = ? AND dia_semana IN (?, 8)
               ORDER BY dia_semana DESC""", (setor_id, dow)).fetchall()
        dem = {}
        for r in rows:
            dem.setdefault(r["hora"], r["necessarios"])
        return dem

    def _turno_cobre(self, turno_id, hora):
        """Verifica se um turno cobre determinada hora (considera o intervalo)."""
        t = self.db.execute(
            "SELECT entrada, saida, intervalo FROM turnos WHERE id=?", (turno_id,)).fetchone()
        if not t:
            return False
        ini = int(t["entrada"].split(":")[0])
        fim = int(t["saida"].split(":")[0])
        if fim <= ini:  # vira a noite
            dentro = hora >= ini or hora < fim
        else:
            dentro = ini <= hora < fim
        if not dentro:
            return False
        # desconsidera o horário de intervalo (funcionário em descanso)
        if t["intervalo"] and "as" in (t["intervalo"] or "").lower():
            try:
                partes = (t["intervalo"] or "").lower().split("as")
                ini_i = int(partes[0].strip().split(":")[0])
                fim_i = int(partes[1].strip().split(":")[0])
                if fim_i <= ini_i:
                    no_intervalo = hora >= ini_i or hora < fim_i
                else:
                    no_intervalo = ini_i <= hora < fim_i
                if no_intervalo:
                    return False
            except (ValueError, IndexError):
                pass
        return True

    def otimizar_por_pico(self, datas_dias, tipo, setor_id,
                          modo="por_pico", peso=None, seed=None):
        """Gera alocações para cobrir a demanda por faixa horária.
        Para cada dia, para cada hora, alcança o nº necessário alocando
        funcionários cujo turno cobre aquela hora."""
        if seed is not None:
            self.rng.seed(seed)
        peso = peso or {}
        alocacoes = []
        setores_alvo = ([setor_id] if setor_id else
                        [r["id"] for r in self.db.execute("SELECT id FROM setores").fetchall()])

        for dia in datas_dias:
            # controle de dupla alocação DENTRO do dia gerado (simulação limpa,
            # não bloqueada pela escala já existente)
            alocados_globais = set()
            for sid in setores_alvo:
                dem = self._demanda_por_hora(sid, dia)
                if not dem:
                    continue
                alocados = set(alocados_globais)
                # horas em ordem de maior demanda (prioriza pico)
                horas_ordenadas = sorted(dem.keys(), key=lambda h: -dem[h])

                for hora in horas_ordenadas:
                    necessarios = dem[hora]
                    cobertos = 0
                    for fid in alocados:
                        t = self.db.execute(
                            "SELECT turno_id FROM funcionario_turno WHERE funcionario_id=?",
                            (fid,)).fetchone()
                        if t and self._turno_cobre(t["turno_id"], hora):
                            cobertos += 1
                    falta = necessarios - cobertos
                    if falta <= 0:
                        continue
                    cands = self._candidatos(sid, dia, tipo)
                    cands = [c for c in cands if c["id"] not in alocados]
                    cands = [c for c in cands if c.get("turno_id") and self._turno_cobre(c["turno_id"], hora)]
                    cands.sort(key=lambda c: self._escore(c, peso, modo, None, dia))
                    for c in cands[:falta]:
                        alocacoes.append({
                            "data": _iso(dia), "tipo": tipo,
                            "funcionario_id": c["id"], "nome": c["nome"],
                            "status": "rascunho", "hora_alvo": hora,
                        })
                        alocados.add(c["id"])
                        alocados_globais.add(c["id"])
        return alocacoes, len(alocacoes)

    def persistir(self, alocacoes, status="rascunho"):
        """Insere alocações no banco e retorna quantas foram criadas."""
        criadas = 0
        for a in alocacoes:
            cur = self.db.execute(
                """INSERT OR IGNORE INTO escala (data, tipo, funcionario_id, status)
                   VALUES (?, ?, ?, ?)""",
                (a["data"], a["tipo"], a["funcionario_id"], status))
            if cur.rowcount and cur.rowcount > 0:
                criadas += 1
        self.db.commit()
        return criadas

    def explicar(self, func_id, data_iso, setor_id, tipo):
        """Explicação auditável: por que este funcionário foi alocado."""
        db = self.db
        f = db.execute(
            "SELECT f.nome, s.nome setor FROM funcionarios f "
            "LEFT JOIN setores s ON s.id = f.setor_id WHERE f.id=?",
            (func_id,)).fetchone()
        m = self._metrica(func_id)
        motivos = []
        motivos.append(f"ativo e habilitado em {f['setor']}")
        if m["domingos"] == 0:
            motivos.append("sem domingos acumulados nesse ciclo")
        if m["horas_extras"] == 0:
            motivos.append("sem hora extra prevista")
        elif m["horas_extras"] <= 3:
            motivos.append("baixo acúmulo de horas extras")
        if m["total"] <= 5:
            motivos.append("menor quantidade total de escalas")
        return {"nome": f["nome"], "setor": f["setor"], "motivos": motivos, "metricas": m}