"""
ScalePro — Base Demonstrativa (dados fictícios, somente para portfólio).

Popula o banco com uma operação coerente de supermercado:
  - 70 funcionários fictícios distribuídos por 5 setores
  - turnos padrão + turnos intermediários (para atender picos)
  - demanda realista por faixa horária (base) e por dia da semana
  - ausências de teste (saem da disponibilidade automaticamente)
  - feriados/calendário com impacto
  - escala de referência gerada de forma coerente com a demanda

Nenhum dado de pessoa real é usado.
"""

import random
import sqlite3
from datetime import date, timedelta

from werkzeug.security import generate_password_hash
from analise import AnaliseEscala, faixas_do_turno, FAIXAS

# Distribuição dos setores conforme especificação
SETORES = [
    (1, "Operação de Caixa", 24),
    (2, "Açougue", 10),
    (3, "Estoque", 14),
    (4, "Hortifruti", 11),
    (5, "Padaria", 11),
]

CARGOS = {
    "Operação de Caixa": ["Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa", "Operador de Caixa",
                          "Operador de Caixa", "Operador de Caixa",
                          "Fiscal de Caixa", "Fiscal de Caixa", "Fiscal de Caixa",
                          "Líder de Frente de Loja"],
    "Açougue": ["Açougueiro", "Açougueiro", "Açougueiro",
                "Auxiliar de Açougue", "Auxiliar de Açougue", "Auxiliar de Açougue",
                "Auxiliar de Açougue", "Balconista", "Balconista", "Balconista"],
    "Estoque": ["Repositor", "Repositor", "Repositor", "Repositor", "Repositor",
                "Repositor", "Repositor", "Estoquista", "Estoquista", "Estoquista",
                "Conferente", "Conferente", "Auxiliar de Estoque", "Auxiliar de Estoque"],
    "Hortifruti": ["Repositor de Hortifruti", "Repositor de Hortifruti", "Repositor de Hortifruti",
                   "Repositor de Hortifruti", "Repositor de Hortifruti",
                   "Auxiliar de Hortifruti", "Auxiliar de Hortifruti", "Auxiliar de Hortifruti",
                   "Auxiliar de Hortifruti", "Auxiliar de Hortifruti", "Líder de Hortifruti"],
    "Padaria": ["Padeiro", "Padeiro", "Padeiro", "Auxiliar de Padaria", "Auxiliar de Padaria",
                "Auxiliar de Padaria", "Auxiliar de Padaria", "Balconista de Padaria",
                "Balconista de Padaria", "Balconista de Padaria", "Confeiteiro"],
}

# Turnos: 8h de trabalho + 2h de intervalo (10h de vão) — jornada realista
TURNOS = [
    ("Manhã", "06:00", "16:00", "12:00 as 14:00"),
    ("Manhã estendida", "07:00", "17:00", "12:00 as 14:00"),
    ("Manhã 2", "08:00", "18:00", "13:00 as 15:00"),
    ("Meio", "09:00", "19:00", "13:00 as 15:00"),
    ("Tarde", "10:00", "20:00", "14:00 as 16:00"),
    ("Intermediária", "11:00", "21:00", "15:00 as 17:00"),
    ("Fechamento", "12:00", "22:00", "16:00 as 18:00"),
]

# Sobrenomes e nomes fictícios brasileiros plausíveis
NOMES_M = ["João", "José", "Carlos", "Pedro", "Lucas", "Marcos", "Paulo", "Rafael",
           "André", "Diego", "Felipe", "Gustavo", "Bruno", "Rodrigo", "Thiago",
           "Ricardo", "Vinícius", "Eduardo", "Fernando", "Marcelo"]
NOMES_F = ["Maria", "Ana", "Julia", "Fernanda", "Patrícia", "Camila", "Larissa",
           "Beatriz", "Carolina", "Mariana", "Renata", "Aline", "Vanessa", "Tatiane",
           "Luciana", "Sabrina", "Priscila", "Débora", "Cristiane", "Juliana"]
SOBRENOMES = ["Silva", "Souza", "Oliveira", "Santos", "Costa", "Pereira", "Almeida",
              "Nascimento", "Lima", "Araújo", "Ribeiro", "Carvalho", "Gomes", "Martins",
              "Rocha", "Barbosa", "Ramos", "Correia", "Mendes", "Freitas"]

# Demanda base por setor por faixa (dia_semana=8 = todos os dias)
DEMANDA_BASE = {
    "Operação de Caixa": {"06-08": 4, "08-10": 6, "10-12": 9, "12-14": 12,
                          "14-16": 9, "16-18": 14, "18-20": 18, "20-22": 10},
    "Açougue": {"06-08": 3, "08-10": 4, "10-12": 7, "12-14": 6,
                "14-16": 4, "16-18": 6, "18-20": 7, "20-22": 3},
    "Estoque": {"06-08": 8, "08-10": 8, "10-12": 5, "12-14": 4,
                "14-16": 6, "16-18": 5, "18-20": 4, "20-22": 3},
    "Hortifruti": {"06-08": 6, "08-10": 7, "10-12": 5, "12-14": 4,
                   "14-16": 4, "16-18": 6, "18-20": 7, "20-22": 4},
    "Padaria": {"06-08": 8, "08-10": 8, "10-12": 5, "12-14": 4,
                "14-16": 4, "16-18": 6, "18-20": 7, "20-22": 3},
}

# Ausências de teste: (tipo, quantidade)
AUSENCIAS = [("Falta", 2), ("Atestado", 2), ("Férias", 2), ("Treinamento", 1),
             ("Afastamento", 1), ("Atraso", 2)]

# Calendário oficial de Boa Vista/RR 2026 (seção 7)
CALENDARIO_BV = [
    ("2026-01-01", "Confraternização Universal", "Feriado", "Nacional"),
    ("2026-01-20", "São Sebastião (padroeiro)", "Feriado", "Municipal"),
    ("2026-04-03", "Paixão de Cristo", "Ponto Facultativo", "Nacional"),
    ("2026-04-21", "Tiradentes", "Feriado", "Nacional"),
    ("2026-05-01", "Dia do Trabalho", "Feriado", "Nacional"),
    ("2026-06-04", "Corpus Christi", "Feriado", "Municipal"),
    ("2026-06-29", "São Pedro", "Ponto Facultativo", "Estadual"),
    ("2026-07-09", "Aniversário de Boa Vista", "Feriado", "Municipal"),
    ("2026-09-07", "Independência do Brasil", "Feriado", "Nacional"),
    ("2026-10-05", "Aniversário de Roraima", "Feriado", "Estadual"),
    ("2026-10-12", "Nossa Senhora Aparecida", "Feriado", "Nacional"),
    ("2026-11-02", "Finados", "Feriado", "Nacional"),
    ("2026-11-15", "Proclamação da República", "Feriado", "Nacional"),
    ("2026-11-20", "Consciência Negra", "Feriado", "Estadual"),
    ("2026-12-08", "Nossa Senhora da Conceição", "Ponto Facultativo", "Municipal"),
    ("2026-12-25", "Natal", "Feriado", "Nacional"),
]


def _limpar_base(db):
    """Remove os dados demonstrativos preservando usuários e setores base."""
    for t in ["escala", "ausencias", "bloqueios", "funcionario_habilidade",
              "funcionario_turno", "funcionarios", "demanda_faixa",
              "demanda_hora_setor", "demanda", "turnos", "feriado_impacto", "calendario"]:
        db.execute(f"DELETE FROM {t}")
    db.execute("DELETE FROM config WHERE chave LIKE 'promocao_%'")
    db.commit()


def _cria_setores(db):
    # garante os 5 setores padrão (por nome)
    nomes = [s[1] for s in SETORES]
    existentes = [r["nome"] for r in db.execute("SELECT nome FROM setores").fetchall()]
    for nome in nomes:
        if nome not in existentes:
            db.execute("INSERT INTO setores (nome) VALUES (?)", (nome,))
    db.commit()
    mapa = {r["nome"]: r["id"] for r in db.execute("SELECT id, nome FROM setores").fetchall()}
    return mapa


def _cria_turnos(db):
    ids = {}
    for nome, e, s, iv in TURNOS:
        cur = db.execute(
            "INSERT OR IGNORE INTO turnos (nome, entrada, saida, intervalo) VALUES (?,?,?,?)",
            (nome, e, s, iv))
        # reutiliza id se já existia com o mesmo nome
        row = db.execute("SELECT id FROM turnos WHERE nome=?", (nome,)).fetchone()
        ids[nome] = row["id"] if row else cur.lastrowid
    db.commit()
    return ids


def _cria_funcionarios(db, setores, turnos):
    """Gera 70 funcionários fictícios e distribui por setor/turno/cargo."""
    nomes_usados = set()
    def nome_unico(sexo):
        lista = NOMES_M if sexo == "M" else NOMES_F
        while True:
            n = random.choice(lista) + " " + random.choice(SOBRENOMES)
            if n not in nomes_usados:
                nomes_usados.add(n)
                return n

    seq = 1
    admissao_ini = date(2018, 1, 1)
    for (_, nome_setor, qtd) in SETORES:
        sid = setores[nome_setor]
        cargos = CARGOS[nome_setor]
        for i in range(qtd):
            cargo = cargos[i]
            sexo = "F" if nome_setor in ("Operação de Caixa", "Hortifruti") and i % 2 == 0 else ("M")
            nome = nome_unico(sexo)
            # turno: mistura padrão e intermediários conforme setor
            turno = _turno_para(nome_setor, i)
            matricula = f"{2026}{seq:04d}"
            admissao = admissao_ini + timedelta(days=random.randint(0, 2600))
            db.execute(
                """INSERT INTO funcionarios (nome, matricula, cargo, setor_id, admissao, carga_horas, ativo)
                   VALUES (?,?,?,?,?,?,1)""",
                (nome, matricula, cargo, sid, admissao.isoformat(), 8))
            fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO funcionario_turno (funcionario_id, turno_id) VALUES (?,?)",
                       (fid, turnos[turno]))
            seq += 1
    db.commit()
    return seq - 1


def _turno_para(nome_setor, i):
    """Atribui turno de forma realista, priorizando os picos de cada setor."""
    if nome_setor == "Operação de Caixa":
        # pico 16-20: mais gente no Tarde/Intermediária/Fechamento
        padrao = ["Manhã", "Tarde", "Fechamento", "Manhã", "Intermediária",
                  "Fechamento", "Manhã estendida", "Tarde", "Meio", "Fechamento",
                  "Manhã", "Tarde"]
        return padrao[i % len(padrao)]
    if nome_setor == "Açougue":
        return ["Manhã", "Manhã", "Tarde", "Fechamento", "Manhã estendida",
                "Intermediária", "Fechamento", "Meio", "Tarde", "Manhã"][i]
    if nome_setor == "Estoque":
        # estoque concentra de manhã (06-10 é pico)
        return ["Manhã", "Manhã", "Manhã", "Manhã estendida", "Manhã 2",
                "Manhã", "Tarde", "Meio", "Tarde", "Fechamento",
                "Manhã 2", "Tarde", "Manhã estendida", "Manhã"][i]
    if nome_setor == "Hortifruti":
        return ["Manhã", "Manhã", "Tarde", "Fechamento", "Manhã estendida",
                "Manhã 2", "Tarde", "Meio", "Intermediária", "Fechamento", "Manhã"][i]
    # Padaria (06-10 e 16-20)
    return ["Manhã", "Manhã", "Manhã", "Manhã estendida", "Manhã 2",
            "Tarde", "Meio", "Intermediária", "Fechamento", "Tarde", "Manhã"][i]


def _cria_demanda(db, setores):
    """Cadastra a demanda base por faixa (dia_semana=8) e também a demanda
    por hora (para o modo 'por_pico' do motor)."""
    for nome_setor, dem in DEMANDA_BASE.items():
        sid = setores[nome_setor]
        for fx, n in dem.items():
            db.execute(
                "INSERT OR IGNORE INTO demanda_faixa (setor_id, dia_semana, faixa, necessarios) VALUES (?,8,?,?)",
                (sid, fx, n))
        # converte faixa -> hora (valor constante dentro da faixa) p/ demanda_hora_setor
        for fx, n in dem.items():
            ini = int(fx.split("-")[0])
            for h in range(ini, ini + 2):
                db.execute(
                    "INSERT OR IGNORE INTO demanda_hora_setor (setor_id, dia_semana, hora, necessarios) VALUES (?,8,?,?)",
                    (sid, h, n))
        # demanda por período (Manhã/Tarde/Noite) e demanda_hora global, coerentes
        periodo = {"Manhã": 0, "Tarde": 0, "Noite": 0}
        for fx, n in dem.items():
            ini = int(fx.split("-")[0])
            if ini < 12:
                periodo["Manhã"] += n
            elif ini < 18:
                periodo["Tarde"] += n
            else:
                periodo["Noite"] += n
        for p, v in periodo.items():
            db.execute(
                "INSERT OR REPLACE INTO demanda (setor_id, periodo, necessarios) VALUES (?,?,?)",
                (sid, p, max(1, v)))
        # demanda_hora global (agrega por hora, todos os setores)
        for fx, n in dem.items():
            ini = int(fx.split("-")[0])
            for h in range(ini, ini + 2):
                db.execute(
                    "INSERT OR IGNORE INTO demanda_hora (hora, necessarios) VALUES (?,0)", (h,))
                db.execute("UPDATE demanda_hora SET necessarios = necessarios + ? WHERE hora = ?",
                           (n, h))
    db.commit()


def _cria_feriados(db, setores):
    for (data, nome, tipo, esfera) in CALENDARIO_BV:
        db.execute(
            """INSERT OR IGNORE INTO calendario (nome, data, tipo, esfera, fonte, ano, recorrencia, ativo)
               VALUES (?,?,?,?,?,?,?,1)""",
            (nome, data, tipo, esfera, "Boa Vista/RR 2026", int(data[:4]), "anual"))
    db.commit()


def _cria_ausencias(db, setores):
    """Cria ausências de teste em datas futuras próximas, em pessoas reais do banco."""
    funcs = db.execute(
        "SELECT f.id, f.setor_id, t.nome turno FROM funcionarios f "
        "LEFT JOIN funcionario_turno ft ON ft.funcionario_id=f.id "
        "LEFT JOIN turnos t ON t.id=ft.turno_id WHERE f.ativo=1").fetchall()
    hoje = date.today()
    rng = random.Random(2026)
    rng.shuffle(list(funcs))
    idx = 0
    for tipo, qtd in AUSENCIAS:
        for _ in range(qtd):
            if idx >= len(funcs):
                break
            f = funcs[idx]
            idx += 1
            # datas futuras espalhadas em até 20 dias
            d = hoje + timedelta(days=rng.randint(1, 20))
            db.execute(
                """INSERT INTO ausencias (funcionario_id, tipo, data, observacao, criado_em)
                   VALUES (?,?,?,?,?)""",
                (f["id"], tipo, d.isoformat(), "Base demonstrativa", hoje.isoformat()))
    db.commit()


def _gera_escala(db, setores, turnos, hoje):
    """Gera escala de referência para as próximas 4 semanas, orientada pela
    demanda: chama mais gente nos turnos que cobrem os picos. Não cria dupla
    alocação e respeita ausências/folgas. Resultado: cobertura realista
    (próxima de 100% com alguns déficits a corrigir)."""
    analise = AnaliseEscala(db)
    funcs = db.execute(
        """SELECT f.id, f.setor_id, t.nome turno_nome, t.entrada, t.saida
           FROM funcionarios f
           LEFT JOIN funcionario_turno ft ON ft.funcionario_id=f.id
           LEFT JOIN turnos t ON t.id=ft.turno_id
           WHERE f.ativo=1""").fetchall()
    turno_info = {r["nome"]: r for r in db.execute("SELECT * FROM turnos").fetchall()}
    fx_por_turno = {r["nome"]: faixas_do_turno(r["entrada"], r["saida"], r["intervalo"]) for r in turno_info.values()}

    dia_atual = hoje
    rng = random.Random(20260915)
    for _ in range(28):
        d_iso = dia_atual.isoformat()
        indisp = {r["funcionario_id"] for r in db.execute(
            "SELECT funcionario_id FROM ausencias WHERE data=? "
            "UNION SELECT funcionario_id FROM bloqueios WHERE data=?",
            (d_iso, d_iso)).fetchall()}
        for nome_setor, sid in setores.items():
            # demanda final do setor no dia (já aplica dia da semana/feriado)
            dem = analise.demanda_final(sid, d_iso)
            if not dem:
                continue
            # elegíveis (do setor, ativos, sem ausência, com turno)
            elegiveis = [f for f in funcs if f["setor_id"] == sid and f["id"] not in indisp
                         and f["turno_nome"]]
            if not elegiveis:
                continue
            # meta de cobertura por faixa: ~92% com jitter (gera déficits reais, não absurdos)
            meta = {fx: max(0, int(round(n * rng.uniform(0.85, 1.0)))) for fx, n in dem.items()}
            chamados = set()
            # preenche cada faixa do mais deficitário ao mais folgado
            for fx in sorted(meta, key=lambda k: -meta[k]):
                preciso = meta[fx]
                cobre = [f for f in elegiveis if f["id"] not in chamados and fx in fx_por_turno.get(f["turno_nome"], [])]
                rng.shuffle(cobre)
                n_cobrindo = len(cobre)
                # chama até atingir a meta, com folga para quem já cobre outras faixas
                usados = [f for f in chamados if fx in fx_por_turno.get(
                    next((x["turno_nome"] for x in elegiveis if x["id"] == f), ""), [])]
                ja = len(usados)
                faltam = preciso - ja
                for f in cobre[:max(0, faltam)]:
                    chamados.add(f["id"])
            for fid in chamados:
                db.execute(
                    "INSERT OR IGNORE INTO escala (data, tipo, funcionario_id, status) VALUES (?,?,?,?)",
                    (d_iso, "semana", fid, "publicada"))
        dia_atual += timedelta(days=1)
    db.commit()


def carregar_base_demonstrativa(db):
    """Destrutivo e idempotente: limpa e recria a base demonstrativa.
    Retorna um resumo para auditoria."""
    _limpar_base(db)
    setores = _cria_setores(db)
    turnos = _cria_turnos(db)
    total_func = _cria_funcionarios(db, setores, turnos)
    _cria_demanda(db, setores)
    _cria_feriados(db, setores)
    _cria_ausencias(db, setores)
    _gera_escala(db, setores, turnos, date.today())
    # config de origem e identidade
    db.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES ('nome_empresa', 'Supermercado Ribeiros')")
    db.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES ('base_demonstrativa', '1')")
    db.execute("INSERT OR REPLACE INTO config (chave, valor) VALUES ('base_demonstrativa_carregada', ?)",
               (date.today().isoformat(),))
    db.commit()
    return {"funcionarios": total_func, "setores": len(setores), "turnos": len(turnos),
            "data": date.today().isoformat()}


if __name__ == "__main__":
    db = sqlite3.connect("escala.db")
    db.row_factory = sqlite3.Row
    res = carregar_base_demonstrativa(db)
    print("Base demonstrativa carregada:", res)
    db.close()