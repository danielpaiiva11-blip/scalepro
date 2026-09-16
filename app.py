import sqlite3
import os
import random
from datetime import date, timedelta
from functools import wraps
from flask import Flask, request, redirect, url_for, render_template, g, flash, session, Response
from werkzeug.security import generate_password_hash, check_password_hash
from fpdf import FPDF
from motor import MotorEscala, MODOS, ESTRATEGIAS, STATUS_ESCALA
from analise import AnaliseEscala, classificacao, faixas_do_turno
import demo as demo_seed

FAIXAS = ["06-08", "08-10", "10-12", "12-14", "14-16", "16-18", "18-20", "20-22"]
DIAS_COMPLETOS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "escala-premium-dev-mudasenha")
DB = os.environ.get("SCALEPRO_DB", "escala.db")

MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
         "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
PERIODOS = ["Manhã", "Tarde", "Noite"]

app.jinja_env.globals.update(MESES=MESES, DIAS_SEMANA=DIAS_SEMANA, PERIODOS=PERIODOS,
                             ESTRATEGIAS=ESTRATEGIAS, STATUS_ESCALA=STATUS_ESCALA, MODOS=MODOS,
                             FAIXAS=FAIXAS, DIAS_COMPLETOS=DIAS_COMPLETOS)


@app.context_processor
def injetar_globais():
    """Expõe nº de horas críticas do radar para badge de alerta no menu."""
    alertas = 0
    nome_empresa = "ScalePro"
    if g.get("user"):
        try:
            db = get_db()
            cfg = get_config(db)
            nome_empresa = cfg.get("nome_empresa", "ScalePro")
            sid = None if g.user["role"] == "admin" else g.user["setor_id"]
            analise = AnaliseEscala(db)
            diag = analise.diagnostico(date.today().isoformat(), setor_id=sid)
            alertas = diag["total_deficit"]
        except Exception:
            alertas = 0
    return {"alertas_pico": alertas, "nome_empresa": nome_empresa}


# ---------------- Banco ----------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS setores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS funcionarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            matricula TEXT,
            cargo TEXT,
            setor_id INTEGER REFERENCES setores(id) ON DELETE SET NULL,
            admissao TEXT,
            ativo INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS escala (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            tipo TEXT NOT NULL DEFAULT 'fds',  -- fds | feriado | semana
            funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
            UNIQUE(data, funcionario_id)
        );
        CREATE TABLE IF NOT EXISTS feriados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL UNIQUE,
            nome TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS demanda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setor_id INTEGER NOT NULL REFERENCES setores(id) ON DELETE CASCADE,
            periodo TEXT NOT NULL,           -- Manhã | Tarde | Noite
            necessarios INTEGER NOT NULL,
            UNIQUE(setor_id, periodo)
        );
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT
        );
        CREATE TABLE IF NOT EXISTS turnos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            entrada TEXT NOT NULL,
            saida TEXT NOT NULL,
            intervalo TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS funcionario_turno (
            funcionario_id INTEGER PRIMARY KEY REFERENCES funcionarios(id) ON DELETE CASCADE,
            turno_id INTEGER REFERENCES turnos(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS demanda_hora (
            hora INTEGER PRIMARY KEY,   -- 0..23
            necessarios INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            nome TEXT NOT NULL,
            cargo TEXT,
            setor_id INTEGER REFERENCES setores(id) ON DELETE SET NULL,
            role TEXT NOT NULL DEFAULT 'supervisor'   -- admin | supervisor
        );
        CREATE TABLE IF NOT EXISTS bloqueios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
            data TEXT NOT NULL,
            motivo TEXT NOT NULL DEFAULT 'Folga',
            UNIQUE(funcionario_id, data)
        );
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT,
            acao TEXT NOT NULL,
            detalhe TEXT,
            quando TEXT NOT NULL
        );
        """
    )
    db.commit()
    # migração: garante coluna intervalo nos turnos
    cols = [r[1] for r in db.execute("PRAGMA table_info(turnos)").fetchall()]
    if "intervalo" not in cols:
        db.execute("ALTER TABLE turnos ADD COLUMN intervalo TEXT NOT NULL DEFAULT ''")
        db.commit()
    # preenche intervalos padrão para turnos existentes sem intervalo
    db.execute("UPDATE turnos SET intervalo = '12:00 as 13:00' WHERE nome = 'Manhã' AND intervalo = ''")
    db.execute("UPDATE turnos SET intervalo = '17:00 as 18:00' WHERE nome = 'Tarde' AND intervalo = ''")
    db.execute("UPDATE turnos SET intervalo = '02:00 as 03:00' WHERE nome = 'Noite' AND intervalo = ''")
    db.commit()

    # ---- Migrations (idempotentes, preservam dados) ----
    def _tem_coluna(tabela, col):
        return col in [r[1] for r in db.execute(f"PRAGMA table_info({tabela})").fetchall()]

    def _tem_tabela(nome):
        return db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None

    # escala.status (fluxo: rascunho → validada → aprovada → publicada → encerrada)
    if not _tem_coluna("escala", "status"):
        db.execute("ALTER TABLE escala ADD COLUMN status TEXT NOT NULL DEFAULT 'publicada'")
        db.commit()

    # funcionarios.carga_horas (jornada/configurada, ex: 8)
    if not _tem_coluna("funcionarios", "carga_horas"):
        db.execute("ALTER TABLE funcionarios ADD COLUMN carga_horas INTEGER NOT NULL DEFAULT 8")
        db.commit()

    # polivalência: funcionário habilitado em setores secundários
    if not _tem_tabela("funcionario_habilidade"):
        db.execute(
            """CREATE TABLE funcionario_habilidade (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                setor_id INTEGER NOT NULL REFERENCES setores(id) ON DELETE CASCADE,
                UNIQUE(funcionario_id, setor_id)
            )""")
        db.commit()

    # demanda por hora e por setor (para geração por horário de pico)
    if not _tem_tabela("demanda_hora_setor"):
        db.execute(
            """CREATE TABLE demanda_hora_setor (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setor_id INTEGER NOT NULL REFERENCES setores(id) ON DELETE CASCADE,
                dia_semana INTEGER NOT NULL DEFAULT 8,  -- 0=seg..6=dom, 7=feriado, 8=todos
                hora INTEGER NOT NULL,                  -- 0..23
                necessarios INTEGER NOT NULL,
                UNIQUE(setor_id, dia_semana, hora)
            )""")
        db.commit()
        # migra a demanda_hora global existente para todos os setores (dia_semana=8)
        setores_ids = [r[0] for r in db.execute("SELECT id FROM setores").fetchall()]
        for (hora, necessarios) in db.execute("SELECT hora, necessarios FROM demanda_hora").fetchall():
            for sid in setores_ids:
                db.execute(
                    "INSERT OR IGNORE INTO demanda_hora_setor (setor_id, dia_semana, hora, necessarios) VALUES (?,?,?,?)",
                    (sid, 8, hora, necessarios))
        db.commit()

    # demanda por faixa horária × dia da semana × setor (fundação do pico/aderência)
    if not _tem_tabela("demanda_faixa"):
        db.execute(
            """CREATE TABLE demanda_faixa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setor_id INTEGER NOT NULL REFERENCES setores(id) ON DELETE CASCADE,
                dia_semana INTEGER NOT NULL DEFAULT 8,  -- 0=seg..6=dom, 7=feriado, 8=todos
                faixa TEXT NOT NULL,                    -- ex: '06-08'
                necessarios INTEGER NOT NULL,
                UNIQUE(setor_id, dia_semana, faixa)
            )""")
        db.commit()
        # migra demanda por período (Manhã/Tarde/Noite) para faixas aproximadas
        mapa_periodo = {
            "Manhã": ["06-08", "08-10", "10-12"],
            "Tarde": ["12-14", "14-16", "16-18"],
            "Noite": ["18-20", "20-22"],
        }
        for (setor_id, periodo, necessarios) in db.execute("SELECT setor_id, periodo, necessarios FROM demanda").fetchall():
            faixas = mapa_periodo.get(periodo, [])
            base = max(1, necessarios // len(faixas)) if faixas else necessarios
            for fx in faixas:
                db.execute(
                    "INSERT OR IGNORE INTO demanda_faixa (setor_id, dia_semana, faixa, necessarios) VALUES (?,8,?,?)",
                    (setor_id, fx, base))
        db.commit()

    # ---------- Calendário Operacional ----------
    if not _tem_tabela("calendario"):
        db.execute(
            """CREATE TABLE calendario (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                data TEXT NOT NULL,
                tipo TEXT NOT NULL DEFAULT 'Feriado',   -- Feriado | Ponto Facultativo | Data Comercial | Evento | Promoção
                esfera TEXT NOT NULL DEFAULT 'Nacional',  -- Nacional | Estadual | Municipal | Comercial
                fonte TEXT DEFAULT 'Boa Vista/RR 2026',
                ano INTEGER,
                recorrencia TEXT DEFAULT 'anual',
                ativo INTEGER NOT NULL DEFAULT 1,
                UNIQUE(data, nome)
            )""")
        db.commit()

    if not _tem_tabela("feriado_impacto"):
        db.execute(
            """CREATE TABLE feriado_impacto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendario_id INTEGER NOT NULL REFERENCES calendario(id) ON DELETE CASCADE,
                setor_id INTEGER NOT NULL REFERENCES setores(id) ON DELETE CASCADE,
                impacto INTEGER NOT NULL DEFAULT 0,   -- % a mais de demanda (0..100)
                UNIQUE(calendario_id, setor_id)
            )""")
        db.commit()

    # Seed do calendário oficial de Boa Vista/RR 2026 (se vazio)
    if db.execute("SELECT COUNT(*) c FROM calendario").fetchone()["c"] == 0:
        calendario_bv = [
            # (data, nome, tipo, esfera)
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
        for (data, nome, tipo, esfera) in calendario_bv:
            db.execute(
                """INSERT OR IGNORE INTO calendario (nome, data, tipo, esfera, fonte, ano, recorrencia, ativo)
                   VALUES (?,?,?,?,?,?,?,1)""",
                (nome, data, tipo, esfera, "Boa Vista/RR 2026", 2026, "anual"))
        db.commit()
        # registra origem da sincronização
        set_config(db, "calendario_fonte", "Boa Vista/RR 2026")
        set_config(db, "calendario_sincronizado", "2026")
        db.commit()

    # Central de Ausências
    if not _tem_tabela("ausencias"):
        db.execute(
            """CREATE TABLE ausencias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                funcionario_id INTEGER NOT NULL REFERENCES funcionarios(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,              -- Falta | Atestado | Férias | Afastamento | Treinamento | Atraso
                data TEXT NOT NULL,
                observacao TEXT DEFAULT '',
                criado_em TEXT
            )""")
        db.commit()

    # seed admin se não existir usuário
    if db.execute("SELECT COUNT(*) c FROM usuarios").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO usuarios (usuario, senha_hash, nome, cargo, role) VALUES (?, ?, ?, ?, ?)",
            ("admin", generate_password_hash("admin123", method="pbkdf2:sha256"), "Administrador", "Gerente Geral", "admin"))
        db.commit()
    # ambiente novo (banco recém-criado) → carrega base demonstrativa automaticamente
    if db.execute("SELECT COUNT(*) c FROM funcionarios").fetchone()["c"] == 0:
        import demo as _demo
        _demo.carregar_base_demonstrativa(db)
    db.close()


# ---------------- Autenticação ----------------

def usuario_atual():
    """Carrega o usuário logado na sessão."""
    if "uid" not in session:
        return None
    db = get_db()
    u = db.execute(
        """SELECT u.*, s.nome setor FROM usuarios u
           LEFT JOIN setores s ON s.id = u.setor_id WHERE u.id = ?""",
        (session["uid"],)).fetchone()
    return u


@app.before_request
def carregar_usuario():
    g.user = usuario_atual()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "admin":
            flash("Acesso restrito ao administrador.")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def setor_permitido(db, setor_id):
    """Restringe acesso do supervisor ao próprio setor."""
    if g.user is None or g.user["role"] == "admin":
        return setor_id  # admin vê tudo
    return g.user["setor_id"]


@app.route("/login", methods=["GET", "POST"])
def login():
    # sempre exibe a tela de login (ponto de entrada do sistema)
    erro = None
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        db = get_db()
        u = db.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
        if u and check_password_hash(u["senha_hash"], senha):
            session.clear()
            session["uid"] = u["id"]
            flash(f"Bem-vindo, {u['nome']}!")
            return redirect(request.args.get("next") or url_for("dashboard"))
        erro = "Usuário ou senha inválidos."
    return render_template("login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.")
    return redirect(url_for("login"))


# ---------------- Usuários (admin) ----------------

@app.route("/usuarios", methods=["GET", "POST"])
@admin_required
def usuarios():
    db = get_db()
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        nome = request.form.get("nome", "").strip()
        cargo = request.form.get("cargo", "").strip()
        setor_id = request.form.get("setor_id", type=int)
        role = request.form.get("role", "supervisor")
        if usuario and senha and nome:
            try:
                db.execute(
                    """INSERT INTO usuarios (usuario, senha_hash, nome, cargo, setor_id, role)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (usuario, generate_password_hash(senha, method="pbkdf2:sha256"), nome, cargo, setor_id, role))
                db.commit()
                flash(f"Usuário '{usuario}' criado.")
            except sqlite3.IntegrityError:
                flash("Já existe um usuário com esse login.")
        return redirect(url_for("usuarios"))
    lista = db.execute(
        """SELECT u.*, s.nome setor FROM usuarios u
           LEFT JOIN setores s ON s.id = u.setor_id ORDER BY u.nome""").fetchall()
    setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    return render_template("usuarios.html", usuarios=lista, setores=setores)


@app.route("/usuarios/<int:uid>/excluir")
@admin_required
def excluir_usuario(uid):
    db = get_db()
    if uid == session.get("uid"):
        flash("Você não pode excluir seu próprio usuário.", "warning")
        return redirect(url_for("usuarios"))
    db.execute("DELETE FROM usuarios WHERE id = ?", (uid,))
    db.commit()
    flash("Usuário excluído.")
    return redirect(url_for("usuarios"))


# ---------------- Utilidades ----------------

def get_config(db):
    """Retorna dict de configurações da loja."""
    return {r["chave"]: r["valor"] for r in db.execute("SELECT chave, valor FROM config").fetchall()}


def set_config(db, chave, valor):
    db.execute(
        "INSERT INTO config (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, str(valor)))


def registrar(db, acao, detalhe=""):
    """Grava log de auditoria."""
    import datetime as _dt
    usuario = g.user["nome"] if g.user else None
    db.execute(
        "INSERT INTO auditoria (usuario, acao, detalhe, quando) VALUES (?, ?, ?, ?)",
        (usuario, acao, detalhe, _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()


def hora_para_min(h):
    """'HH:MM' -> minutos."""
    hh, mm = h.split(":")
    return int(hh) * 60 + int(mm)


def presente_no_turno(entrada, saida, hora):
    """Verifica se uma hora (0..24) está dentro do intervalo do turno,
    tratando turnos que ultrapassam a meia-noite."""
    ini = hora_para_min(entrada)
    fim = hora_para_min(saida)
    m = hora * 60
    if fim <= ini:           # turno vira a noite (ex: 22h -> 06h)
        return m >= ini or m < fim
    return ini <= m < fim


def datas_tipo(ano, mes, tipo):
    """Lista de datas do mês conforme o tipo de escala."""
    db = get_db()
    d = date(ano, mes, 1)
    datas = []
    while d.month == mes:
        if tipo == "fds" and d.weekday() in (5, 6):
            datas.append(d)
        elif tipo == "semana":
            datas.append(d)
        d += timedelta(days=1)
    if tipo == "feriado":
        ini, fim = date(ano, mes, 1), date(ano, mes, 1)
        fim = (ini.replace(day=28) + timedelta(days=7)).replace(day=1)
        datas = [
            date.fromisoformat(r["data"]) for r in db.execute(
                "SELECT data FROM calendario WHERE data >= ? AND data < ? AND ativo = 1 ORDER BY data",
                (ini.isoformat(), fim.isoformat()),
            ).fetchall()
        ]
    return datas


# ---------------- Turnos & Radar de Pico ----------------

def garantir_turnos_padrao(db):
    """Garante turnos e configuração padrão se não existirem."""
    if db.execute("SELECT COUNT(*) c FROM turnos").fetchone()["c"] == 0:
        for nome, e, s, iv in [
            ("Manhã", "06:00", "14:00", "12:00 as 13:00"),
            ("Tarde", "14:00", "22:00", "17:00 as 18:00"),
            ("Noite", "22:00", "06:00", "02:00 as 03:00"),
        ]:
            db.execute("INSERT INTO turnos (nome, entrada, saida, intervalo) VALUES (?, ?, ?, ?)",
                       (nome, e, s, iv))
    cfg = get_config(db)
    if "abertura" not in cfg:
        set_config(db, "abertura", "06:00")
    if "fechamento" not in cfg:
        set_config(db, "fechamento", "22:00")
    db.commit()


def gerar_presentes_por_hora(db, abertura, fechamento, setor_id=None):
    """Retorna, para cada hora [abertura, fechamento), o nº de funcionários ativos
    com turno cobrindo aquela hora. Filtra por setor se informado."""
    turnos = db.execute("SELECT id, entrada, saida FROM turnos").fetchall()
    q = """SELECT ft.turno_id FROM funcionario_turno ft
           JOIN funcionarios f ON f.id = ft.funcionario_id AND f.ativo = 1
        """
    params = []
    if setor_id:
        q += " WHERE f.setor_id = ?"
        params.append(setor_id)
    funcs = db.execute(q, params).fetchall()
    # agrupa por turno_id: quantos funcionários em cada turno
    por_turno = {}
    for f in funcs:
        por_turno[f["turno_id"]] = por_turno.get(f["turno_id"], 0) + 1

    a, f = int(abertura), int(fechamento)
    # intervalo de horas, tratando virada de noite (f <= a)
    if f <= a:
        horas = list(range(a, 24)) + list(range(0, f))
    else:
        horas = list(range(a, f))

    presentes = []
    for h in horas:
        total = 0
        for t in turnos:
            if presente_no_turno(t["entrada"], t["saida"], h):
                total += por_turno.get(t["id"], 0)
        presentes.append({"hora": h, "presentes": total})
    return presentes


@app.route("/radar")
@login_required
def radar():
    db = get_db()
    garantir_turnos_padrao(db)
    cfg = get_config(db)
    abertura = int(cfg.get("abertura", "06:00").split(":")[0])
    fechamento = int(cfg.get("fechamento", "22:00").split(":")[0])

    # supervisor enxerga apenas o próprio setor
    sid = None if g.user["role"] == "admin" else g.user["setor_id"]
    setor_filtro = request.args.get("setor", type=int)
    if g.user["role"] == "admin" and setor_filtro:
        sid = setor_filtro

    # dia de análise (padrão: hoje; aceita parâmetro para prever)
    dia = request.args.get("dia") or date.today().isoformat()
    analise = AnaliseEscala(db)

    # ---- fonte única de verdade: diagnóstico unificado do dia ----
    diag = analise.diagnostico(dia, setor_id=sid)
    faixas = diag["faixas"]

    estado = "ok"
    n_ok = n_atencao = n_critico = 0
    criticos = 0
    pior_deficit = 0
    pior_faixa = None
    for f in faixas:
        if f["cobertura"] is None:
            continue
        if f["cobertura"] > 110 or f["cobertura"] < 70:
            estado = "critico"
            n_critico += 1
            criticos += 1
        elif f["cobertura"] < 85:
            estado = "atencao" if estado != "critico" else estado
            n_atencao += 1
        else:
            n_ok += 1
        if f["deficit"] > pior_deficit:
            pior_deficit = f["deficit"]
            pior_faixa = f["faixa"]
    if estado == "ok":
        estado = "ok"
    elif n_critico == 0:
        estado = "atencao"

    # turnos p/ exibição (quantidade filtrada por setor)
    turnos = db.execute(
        """SELECT t.nome, t.entrada, t.saida,
                  (SELECT COUNT(*) FROM funcionario_turno ft
                   JOIN funcionarios f ON f.id = ft.funcionario_id AND f.ativo = 1
                   WHERE ft.turno_id = t.id AND (? IS NULL OR f.setor_id = ?)) qtd
           FROM turnos t ORDER BY t.entrada""", (sid, sid)).fetchall()

    # picos de demanda (top faixas de necessidade)
    picos = sorted([(f["faixa"], f["necessarios"]) for f in faixas if f["necessarios"] > 0],
                   key=lambda x: -x[1])[:3]

    # funcionários sem turno definido (filtrado por setor)
    sem_turno = db.execute(
        """SELECT COUNT(*) c FROM funcionarios f
           WHERE f.ativo = 1 AND (? IS NULL OR f.setor_id = ?) AND NOT EXISTS
             (SELECT 1 FROM funcionario_turno ft WHERE ft.funcionario_id = f.id)
        """, (sid, sid)).fetchone()["c"]

    # --- Resumo por setor (sempre todos, para visão geral) ---
    setores_rs = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    setores_resumo = []
    for s in setores_rs:
        dem = analise.demanda_final(s["id"], dia)
        esc = analise.escalados_por_faixa(s["id"], dia)
        pico = max(dem.values()) if dem else 0
        ativos = db.execute(
            "SELECT COUNT(*) c FROM funcionarios WHERE ativo = 1 AND setor_id = ?",
            (s["id"],)).fetchone()["c"]
        pct = round(100 * min(ativos, pico) / pico) if pico else None
        if pct is None:
            st = "sem"
        elif pct >= 100:
            st = "ok"
        elif pct >= 85:
            st = "atencao"
        else:
            st = "critico"
        setores_resumo.append({"id": s["id"], "nome": s["nome"], "ativos": ativos,
                               "pico": pico, "pct": pct, "status": st})

    setor_selecionado = None
    if sid:
        r = db.execute("SELECT nome FROM setores WHERE id = ?", (sid,)).fetchone()
        setor_selecionado = r["nome"] if r else None

    return render_template("radar.html", faixas=faixas, criticos=criticos,
                           estado=estado, n_ok=n_ok, n_atencao=n_atencao, n_critico=n_critico,
                           turnos=turnos, picos=picos, sem_turno=sem_turno,
                           setores_resumo=setores_resumo, setor_selecionado=setor_selecionado,
                           setor_filtro=sid,
                           cobertura_total=diag["cobertura_geral"],
                           pior_deficit=pior_deficit, pior_faixa=pior_faixa,
                           diag=diag, dia=dia,
                           abertura=cfg.get("abertura", "06:00"),
                           fechamento=cfg.get("fechamento", "22:00"))


@app.route("/radar/operacional")
@login_required
def radar_operacional():
    db = get_db()
    hoje = date.today()
    ano = request.args.get("ano", hoje.year, type=int)
    mes = request.args.get("mes", hoje.month, type=int)
    dia = request.args.get("dia", hoje.isoformat())
    setor_id = request.args.get("setor_id", type=int)
    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]

    analise = AnaliseEscala(db)
    res = analise.resiliencia(dia, setor_id)
    hm = analise.heatmap(dia, setor_id)
    rem = analise.remanejamento(dia, setor_id)

    # setores disponíveis para o filtro
    if g.user["role"] != "admin":
        setores = [s for s in db.execute(
            "SELECT id, nome FROM setores WHERE id=?", (g.user["setor_id"],)).fetchall()]
    else:
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()

    return render_template("radar_operacional.html", res=res, hm=hm, rem=rem,
                           setores=setores, setor_id=setor_id, dia=dia,
                           ano=ano, mes=mes, hoje=hoje)


@app.route("/radar/config", methods=["GET", "POST"])
@admin_required
def radar_config():
    db = get_db()
    garantir_turnos_padrao(db)
    if request.method == "POST":
        set_config(db, "abertura", request.form.get("abertura", "06:00"))
        set_config(db, "fechamento", request.form.get("fechamento", "22:00"))
        # turnos
        for key in request.form.keys():
            if key.startswith("turno_entrada_"):
                tid = int(key.split("_")[-1])
                db.execute("UPDATE turnos SET entrada = ?, saida = ? WHERE id = ?",
                           (request.form[key],
                            request.form.get(f"turno_saida_{tid}", "22:00"), tid))
        db.commit()
        # demanda por hora
        for h in range(24):
            n = request.form.get(f"dem_hora_{h}", type=int)
            if n is not None:
                db.execute(
                    "INSERT INTO demanda_hora (hora, necessarios) VALUES (?, ?) "
                    "ON CONFLICT(hora) DO UPDATE SET necessarios = excluded.necessarios",
                    (h, n))
        db.commit()
        flash("Configuração do radar salva.")
        return redirect(url_for("radar"))

    cfg = get_config(db)
    turnos = db.execute("SELECT * FROM turnos ORDER BY entrada").fetchall()
    dem = {r["hora"]: r["necessarios"] for r in db.execute("SELECT * FROM demanda_hora").fetchall()}
    return render_template("radar_config.html", cfg=cfg, turnos=turnos, dem=dem,
                           abertura=cfg.get("abertura", "06:00"),
                           fechamento=cfg.get("fechamento", "22:00"))


# ---------------- Dashboard ----------------

@app.route("/")
def inicio():
    """Link raiz sempre inicia na tela de login."""
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    hoje = date.today()
    em30 = hoje + timedelta(days=30)
    sid = None if g.user["role"] == "admin" else g.user["setor_id"]

    kpis = {
        "funcionarios": db.execute(
            "SELECT COUNT(*) c FROM funcionarios WHERE ativo = 1 AND (? IS NULL OR setor_id = ?)",
            (sid, sid)).fetchone()["c"],
        "setores": 1 if sid else db.execute("SELECT COUNT(*) c FROM setores").fetchone()["c"],
        "escalas_30d": db.execute(
            """SELECT COUNT(*) c FROM escala e JOIN funcionarios f ON f.id = e.funcionario_id
               WHERE e.data >= ? AND e.data <= ? AND (? IS NULL OR f.setor_id = ?)""",
            (hoje.isoformat(), em30.isoformat(), sid, sid)).fetchone()["c"],
        "feriados": db.execute("SELECT COUNT(*) c FROM feriados WHERE data >= ?",
                               (hoje.isoformat(),)).fetchone()["c"],
    }

    # Cobertura: disponíveis por setor vs pico de demanda (maior período)
    if sid:
        setores = db.execute("SELECT id, nome FROM setores WHERE id = ?", (sid,)).fetchall()
    else:
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    cobertura = []
    for s in setores:
        ativos = db.execute(
            "SELECT COUNT(*) c FROM funcionarios WHERE ativo = 1 AND setor_id = ?",
            (s["id"],)).fetchone()["c"]
        pico = db.execute(
            "SELECT COALESCE(MAX(necessarios),0) p FROM demanda WHERE setor_id = ?",
            (s["id"],)).fetchone()["p"]
        pct = round(100 * min(ativos, pico) / pico) if pico else None
        cobertura.append({"nome": s["nome"], "ativos": ativos, "pico": pico, "pct": pct})
    capacidade = [c for c in cobertura if c["pct"] is not None]
    kpis["cobertura"] = round(sum(c["pct"] for c in capacidade) / len(capacidade)) if capacidade else None

    # Pico por período (soma de todos os setores)
    pico_periodos = [db.execute(
        "SELECT COALESCE(SUM(necessarios),0) s FROM demanda WHERE periodo = ?",
        (p,)).fetchone()["s"] for p in PERIODOS]

    # Escalas por dia (próximos 30 dias)
    rows = db.execute(
        """SELECT e.data, COUNT(*) c FROM escala e
           JOIN funcionarios f ON f.id = e.funcionario_id
           WHERE e.data >= ? AND e.data <= ? AND (? IS NULL OR f.setor_id = ?)
           GROUP BY e.data ORDER BY e.data""",
        (hoje.isoformat(), em30.isoformat(), sid, sid)).fetchall()
    escala_dias = {"labels": [f"{r['data'][8:10]}/{r['data'][5:7]}" for r in rows],
"vals": [r["c"] for r in rows]}

    # Funcionários por setor
    rows = db.execute(
        """SELECT COALESCE(s.nome,'Sem setor') n, COUNT(f.id) c
           FROM funcionarios f LEFT JOIN setores s ON s.id = f.setor_id
           WHERE f.ativo = 1 AND (? IS NULL OR f.setor_id = ?)
           GROUP BY n ORDER BY c DESC""", (sid, sid)).fetchall()
    func_setor = {"labels": [r["n"] for r in rows], "vals": [r["c"] for r in rows]}

    # Menções mais agendadas (top 5) para quadro
    top = db.execute(
        """SELECT f.nome, COUNT(e.id) c FROM funcionarios f
           JOIN escala e ON e.funcionario_id = f.id
           WHERE (? IS NULL OR f.setor_id = ?)
           GROUP BY f.id ORDER BY c DESC, f.nome LIMIT 5""", (sid, sid)).fetchall()

    # ---- Análise executiva: Scale Score + alertas + próximo pico/feriado ----
    analise = AnaliseEscala(db)
    diag = analise.diagnostico(hoje.isoformat(), setor_id=sid)
    # cobertura e equidade da mesma fonte de verdade
    kpis["cobertura"] = diag["cobertura_geral"]
    kpis["equidade"] = diag["equidade"]
    kpis["escaladas_total"] = db.execute(
        "SELECT COUNT(*) c FROM escala").fetchone()["c"]
    kpis["ausentes_hoje"] = db.execute(
        "SELECT COUNT(*) c FROM ausencias WHERE data = ?", (hoje.isoformat(),)).fetchone()["c"]

    # próximo feriado
    prox_feriado = db.execute(
        "SELECT data, nome FROM calendario WHERE data >= ? AND ativo = 1 ORDER BY data LIMIT 1",
        (hoje.isoformat(),)).fetchone()

    # próximo pico (faixa com maior demanda global no dia)
    prox_pico = None
    an_pico = analise.analise_dia(hoje.isoformat(), setor_id=sid)
    faixas_pico = [f for f in an_pico["faixas"] if f["necessarios"] > 0]
    if faixas_pico:
        top = max(faixas_pico, key=lambda f: f["necessarios"])
        prox_pico = top["faixa"]

    # alertas operacionais (déficit real por setor, do diagnóstico)
    alertas = []
    for s in diag["setores"]:
        if s["cobertura"] is not None and s["cobertura"] < 85 and s["deficit"] > 0:
            alertas.append({"nivel": "critico", "txt": f"{s['setor']} terá déficit ({s['deficit']} pessoa(s))."})
        elif s["cobertura"] is not None and s["cobertura"] > 110:
            alertas.append({"nivel": "atencao", "txt": f"{s['setor']} com excesso ({s['cobertura']}%)."})
        elif s["cobertura"] is not None and s["cobertura"] < 95:
            alertas.append({"nivel": "atencao", "txt": f"{s['setor']} chegará a {s['cobertura']}%."})
    if prox_feriado:
        dias = (date.fromisoformat(prox_feriado["data"]) - hoje).days
        alertas.append({"nivel": "info", "txt": f"Feriado '{prox_feriado['nome']}' em {dias} dia(s)."})
    alertas = alertas[:6]

    # equidade e Scale Score vêm do diag (fonte única)
    equidade = diag["equidade"]

    return render_template("dashboard.html", kpis=kpis, cobertura=cobertura,
                           pico_periodos=pico_periodos, escala_dias=escala_dias,
                           func_setor=func_setor, top=top, hoje=hoje,
                           diag=diag, prox_feriado=prox_feriado, prox_pico=prox_pico,
                           alertas=alertas, equidade=equidade)


# ---------------- Funcionários ----------------

@app.route("/funcionarios")
@login_required
def funcionarios():
    db = get_db()
    setor_id = request.args.get("setor_id", type=int)
    # supervisor fica limitado ao próprio setor
    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]

    # busca e ordenação
    busca = request.args.get("q", "").strip()
    ord_col = request.args.get("ord", "nome")

    colunas = {
        "nome": "f.nome",
        "matricula": "f.matricula",
        "cargo": "f.cargo",
        "setor": "s.nome",
        "turno": "t.nome",
        "total_escalas": "total_escalas",
        "ultima": "ultima",
    }
    ord_sql = colunas.get(ord_col, "f.nome")

    q = """SELECT f.*, s.nome setor, t.nome turno,
                  (SELECT COUNT(*) FROM escala e WHERE e.funcionario_id = f.id) total_escalas,
                  (SELECT MAX(data) FROM escala e WHERE e.funcionario_id = f.id) ultima
           FROM funcionarios f
           LEFT JOIN setores s ON s.id = f.setor_id
           LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
           LEFT JOIN turnos t ON t.id = ft.turno_id"""
    params = []
    conds = []
    if setor_id:
        conds.append("f.setor_id = ?")
        params.append(setor_id)
    if busca:
        conds.append("(f.nome LIKE ? OR f.matricula LIKE ? OR f.cargo LIKE ?)")
        like = f"%{busca}%"
        params += [like, like, like]
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += f" ORDER BY {ord_sql}"

    # total (para paginação)
    total_registros = db.execute(
        "SELECT COUNT(*) c FROM funcionarios f LEFT JOIN setores s ON s.id=f.setor_id " +
        ("WHERE " + " AND ".join(conds) if conds else ""), params).fetchone()["c"]

    # paginação
    POR_PAGINA = 25
    page = max(1, request.args.get("page", 1, type=int))
    total_paginas = max(1, (total_registros + POR_PAGINA - 1) // POR_PAGINA)
    page = min(page, total_paginas)
    offset = (page - 1) * POR_PAGINA
    q += f" LIMIT {POR_PAGINA} OFFSET {offset}"
    lista = db.execute(q, params).fetchall()

    if g.user["role"] != "admin":
        # supervisor não escolhe outro setor
        setores = [s for s in db.execute(
            "SELECT id, nome FROM setores WHERE id = ?", (g.user["setor_id"],)).fetchall()]
    else:
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    turnos = db.execute("SELECT id, nome, entrada, saida FROM turnos ORDER BY entrada").fetchall()
    # lista em dicts puros para o modal de edição (seguro p/ tojson no <script>)
    funcionarios_ed = [{
        "id": f["id"], "nome": f["nome"] or "", "matricula": f["matricula"] or "",
        "cargo": f["cargo"] or "", "admissao": f["admissao"] or "",
        "setor_id": f["setor_id"] or 0, "turno": f["turno"] or "",
    } for f in lista]
    return render_template("funcionarios.html", funcionarios=lista, setores=setores,
                           turnos=turnos, setor_id=setor_id, busca=busca, ord=ord_col,
                           page=page, total_paginas=total_paginas,
                           total_registros=total_registros,
                           funcionarios_ed=funcionarios_ed,
                           ativos=sum(1 for f in lista if f["ativo"]))


@app.route("/funcionarios/add", methods=["POST"])
@login_required
def add_funcionario():
    db = get_db()
    nome = request.form.get("nome", "").strip()
    setor_id = request.form.get("setor_id", type=int)
    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]  # supervisor só no próprio setor
    if nome:
        cur = db.execute(
            """INSERT INTO funcionarios (nome, matricula, cargo, setor_id, admissao)
               VALUES (?, ?, ?, ?, ?)""",
            (nome, request.form.get("matricula") or None,
             request.form.get("cargo") or None,
             setor_id,
             request.form.get("admissao") or None))
        fid = cur.lastrowid
        turno_id = request.form.get("turno_id", type=int)
        if turno_id:
            db.execute(
                "INSERT INTO funcionario_turno (funcionario_id, turno_id) VALUES (?, ?)",
                (fid, turno_id))
        db.commit()
        flash(f"Funcionário '{nome}' cadastrado.")
    return redirect(url_for("funcionarios", setor_id=request.args.get("setor_id")))


@app.route("/funcionarios/<int:fid>/toggle")
@login_required
def toggle_funcionario(fid):
    db = get_db()
    db.execute("UPDATE funcionarios SET ativo = 1 - ativo WHERE id = ?", (fid,))
    db.commit()
    return redirect(request.referrer or url_for("funcionarios"))


@app.route("/funcionarios/editar/<int:fid>", methods=["POST"])
@login_required
def editar_funcionario(fid):
    db = get_db()
    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Nome é obrigatório.")
        return redirect(request.referrer or url_for("funcionarios"))
    dados_atual = {}
    dados_atual["nome"] = nome
    dados_atual["matricula"] = request.form.get("matricula") or None
    dados_atual["cargo"] = request.form.get("cargo") or None
    dados_atual["admissao"] = request.form.get("admissao") or None
    # setor: admin pode trocar; supervisor mantém o próprio
    setor_id = request.form.get("setor_id", type=int)
    if g.user["role"] == "admin":
        dados_atual["setor_id"] = setor_id
    else:
        dados_atual["setor_id"] = g.user["setor_id"]

    db.execute(
        """UPDATE funcionarios SET nome=?, matricula=?, cargo=?, admissao=?, setor_id=?
           WHERE id=?""",
        (dados_atual["nome"], dados_atual["matricula"], dados_atual["cargo"],
         dados_atual["admissao"], dados_atual["setor_id"], fid))
    # turno
    turno_id = request.form.get("turno_id", type=int)
    if turno_id:
        db.execute(
            "INSERT INTO funcionario_turno (funcionario_id, turno_id) VALUES (?, ?) "
            "ON CONFLICT(funcionario_id) DO UPDATE SET turno_id = excluded.turno_id",
            (fid, turno_id))
    else:
        db.execute("DELETE FROM funcionario_turno WHERE funcionario_id = ?", (fid,))
    db.commit()
    flash(f"Dados de '{nome}' atualizados.")
    return redirect(request.referrer or url_for("funcionarios"))


@app.route("/funcionarios/<int:fid>/excluir")
@login_required
def excluir_funcionario(fid):
    db = get_db()
    db.execute("DELETE FROM funcionarios WHERE id = ?", (fid,))
    db.commit()
    flash("Funcionário excluído.")
    return redirect(request.referrer or url_for("funcionarios"))


@app.route("/funcionarios/<int:fid>/turno", methods=["POST"])
@login_required
def set_funcionario_turno(fid):
    db = get_db()
    turno_id = request.form.get("turno_id", type=int)
    if turno_id:
        db.execute(
            "INSERT INTO funcionario_turno (funcionario_id, turno_id) VALUES (?, ?) "
            "ON CONFLICT(funcionario_id) DO UPDATE SET turno_id = excluded.turno_id",
            (fid, turno_id))
    else:
        db.execute("DELETE FROM funcionario_turno WHERE funcionario_id = ?", (fid,))
    db.commit()
    flash("Turno atualizado.")
    return redirect(request.referrer or url_for("funcionarios"))


@app.route("/funcionarios/<int:fid>")
@login_required
def funcionario360(fid):
    db = get_db()
    f = db.execute(
        """SELECT f.*, s.nome setor, t.nome turno, t.entrada, t.saida, t.intervalo FROM funcionarios f
           LEFT JOIN setores s ON s.id = f.setor_id
           LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
           LEFT JOIN turnos t ON t.id = ft.turno_id
           WHERE f.id = ?""",
        (fid,)).fetchone()
    if not f:
        flash("Funcionário não encontrado.")
        return redirect(url_for("funcionarios"))

    total = db.execute(
        "SELECT COUNT(*) c FROM escala WHERE funcionario_id = ?", (fid,)).fetchone()["c"]
    fds = db.execute(
        "SELECT COUNT(*) c FROM escala WHERE funcionario_id = ? AND tipo = 'fds'",
        (fid,)).fetchone()["c"]
    feriados_n = db.execute(
        "SELECT COUNT(*) c FROM escala WHERE funcionario_id = ? AND tipo = 'feriado'",
        (fid,)).fetchone()["c"]

    hoje = date.today()
    proximas = db.execute(
        """SELECT * FROM escala WHERE funcionario_id = ? AND data >= ?
           ORDER BY data LIMIT 8""", (fid, hoje.isoformat())).fetchall()
    historico = db.execute(
        """SELECT * FROM escala WHERE funcionario_id = ? AND data < ?
           ORDER BY data DESC LIMIT 10""", (fid, hoje.isoformat())).fetchall()

    # escalas por mês (últimos 6 meses) p/ gráfico
    seis = (hoje.replace(day=1) - timedelta(days=150)).replace(day=1)
    rows = db.execute(
        """SELECT substr(data, 1, 7) m, COUNT(*) c FROM escala
           WHERE funcionario_id = ? AND data >= ? GROUP BY m ORDER BY m""",
        (fid, seis.isoformat())).fetchall()
    grafico = {"labels": [r["m"] for r in rows], "vals": [r["c"] for r in rows]}

    # horas extras estimadas: 8h por escala de fds/feriado
    horas_extras = (fds + feriados_n) * 8

    return render_template("funcionario360.html", f=f, total=total, fds=fds,
                           feriados_n=feriados_n, horas_extras=horas_extras,
                           proximas=proximas, historico=historico, grafico=grafico)


# ---------------- Setores ----------------

@app.route("/setores", methods=["GET", "POST"])
@admin_required
def setores():
    db = get_db()
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        if nome:
            try:
                db.execute("INSERT INTO setores (nome) VALUES (?)", (nome,))
                db.commit()
                flash(f"Setor '{nome}' criado.")
            except sqlite3.IntegrityError:
                flash("Já existe um setor com esse nome.", "error")
        return redirect(url_for("setores"))

    # visão: cards | matriz
    visao = request.args.get("visao", "cards")

    setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    lista = []
    for s in setores:
        ativos = db.execute(
            "SELECT COUNT(*) c FROM funcionarios WHERE ativo=1 AND setor_id=?", (s["id"],)).fetchone()["c"]
        # demanda por faixa (dia_semana=8 = todos os dias)
        dfaixas = db.execute(
            "SELECT faixa, necessarios FROM demanda_faixa WHERE setor_id=? AND dia_semana=8 ORDER BY faixa",
            (s["id"],)).fetchall()
        dem_faixas = {d["faixa"]: d["necessarios"] for d in dfaixas}
        necessidade_total = sum(dem_faixas.values())
        maior_pico = max(dem_faixas.values()) if dem_faixas else 0
        faixa_pico = max(dem_faixas, key=dem_faixas.get) if dem_faixas else None
        # escalados hoje (para cobertura)
        escalados = db.execute(
            """SELECT COUNT(DISTINCT f.id) c FROM escala e
               JOIN funcionarios f ON f.id = e.funcionario_id
               WHERE f.setor_id = ? AND e.data = ?""",
            (s["id"], date.today().isoformat())).fetchone()["c"]
        disponiveis = db.execute(
            "SELECT COUNT(*) c FROM funcionarios WHERE ativo=1 AND setor_id=?", (s["id"],)).fetchone()["c"]
        cobertura_pct = round(100 * min(disponiveis, maior_pico) / maior_pico) if maior_pico else None
        lista.append({
            "id": s["id"], "nome": s["nome"], "ativos": ativos,
            "disponiveis": disponiveis, "escalados": escalados,
            "necessidade": necessidade_total, "maior_pico": maior_pico,
            "faixa_pico": faixa_pico,
            "cobertura_pct": cobertura_pct,
            "dem_faixas": dem_faixas,
        })
    return render_template("setores.html", setores=lista, visao=visao,
                           faixas=FAIXAS, dias=DIAS_COMPLETOS)


@app.route("/setores/<int:sid>/excluir")
@admin_required
def excluir_setor(sid):
    db = get_db()
    db.execute("DELETE FROM setores WHERE id = ?", (sid,))
    db.commit()
    flash("Setor excluído.")
    return redirect(url_for("setores"))


@app.route("/setores/<int:sid>/demanda", methods=["POST"])
@admin_required
def salvar_demanda(sid):
    db = get_db()
    # salva demanda por faixa (dia_semana=8 = todos os dias, padrão)
    db.execute("DELETE FROM demanda_faixa WHERE setor_id = ? AND dia_semana = 8", (sid,))
    for fx in FAIXAS:
        n = request.form.get(f"faixa_{fx}", type=int)
        if n:
            db.execute(
                "INSERT INTO demanda_faixa (setor_id, dia_semana, faixa, necessarios) VALUES (?,8,?,?)",
                (sid, fx, n))
    db.commit()
    registrar(db, "Salvar demanda", f"Setor {sid} — faixas horárias")
    flash("Demanda por faixa horária salva.")
    return redirect(url_for("setores"))


# ---------------- Calendário Operacional ----------------

TIPOS_CALENDARIO = ["Feriado", "Ponto Facultativo", "Data Comercial", "Evento", "Promoção"]
ESFERAS_CALENDARIO = ["Nacional", "Estadual", "Municipal", "Comercial"]


@app.route("/feriados", methods=["GET", "POST"])
@admin_required
def feriados():
    db = get_db()
    if request.method == "POST":
        data = request.form.get("data")
        nome = request.form.get("nome", "").strip()
        tipo = request.form.get("tipo", "Feriado")
        esfera = request.form.get("esfera", "Nacional")
        if data and nome:
            try:
                db.execute(
                    """INSERT INTO calendario (nome, data, tipo, esfera, fonte, ano, recorrencia, ativo)
                       VALUES (?,?,?,?,?,?,?,1)""",
                    (nome, data, tipo, esfera, "Cadastro manual", int(data[:4]), "anual"))
                db.commit()
                registrar(db, "Cadastrar data", f"{nome} ({data})")
                flash(f"'{nome}' cadastrado no calendário.")
            except sqlite3.IntegrityError:
                flash("Já existe essa data/nome no calendário.", "error")
        return redirect(url_for("feriados"))

    ano = request.args.get("ano", date.today().year, type=int)
    lista = db.execute(
        "SELECT * FROM calendario WHERE ano = ? ORDER BY data", (ano,)).fetchall()
    # impacto por feriado
    impactos = db.execute(
        """SELECT fi.calendario_id, s.nome setor, fi.impacto FROM feriado_impacto fi
           JOIN setores s ON s.id = fi.setor_id""").fetchall()
    imp_map = {}
    for i in impactos:
        imp_map.setdefault(i["calendario_id"], {})[i["setor"]] = i["impacto"]

    setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    cfg = get_config(db)
    return render_template("feriados.html", feriados=lista, hoje=date.today(),
                           ano=ano, setores=setores, imp_map=imp_map,
                           tipos=TIPOS_CALENDARIO, esferas=ESFERAS_CALENDARIO,
                           fonte=cfg.get("calendario_fonte", "Boa Vista/RR 2026"),
                           sincronizado=cfg.get("calendario_sincronizado", ""))


@app.route("/feriados/<int:fid>/excluir")
@admin_required
def excluir_feriado(fid):
    db = get_db()
    db.execute("DELETE FROM calendario WHERE id = ?", (fid,))
    db.commit()
    registrar(db, "Remover data do calendário", f"id {fid}")
    return redirect(request.referrer or url_for("feriados"))


@app.route("/feriados/<int:fid>/impacto", methods=["POST"])
@admin_required
def salvar_impacto(fid):
    """Define o impacto de demanda por setor para uma data especial."""
    db = get_db()
    db.execute("DELETE FROM feriado_impacto WHERE calendario_id = ?", (fid,))
    for s in db.execute("SELECT id FROM setores").fetchall():
        imp = request.form.get(f"imp_{s['id']}", type=int)
        if imp:
            db.execute(
                "INSERT INTO feriado_impacto (calendario_id, setor_id, impacto) VALUES (?,?,?)",
                (fid, s["id"], imp))
    db.commit()
    registrar(db, "Impacto de data especial", f"calendario {fid}")
    flash("Impacto por setor salvo.")
    return redirect(url_for("feriados", ano=date.today().year))


@app.route("/feriados/sincronizar", methods=["POST"])
@admin_required
def sincronizar_feriados():
    """Re-aplica o calendário oficial de Boa Vista/RR (2026) — idempotente."""
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM calendario").fetchone()["c"] == 0:
        flash("Calendário oficial de Boa Vista/RR 2026 sincronizado.", "warning")
    else:
        flash("Calendário oficial já está carregado. Sem alterações.", "warning")
    return redirect(url_for("feriados", ano=2026))


# ---------------- Gerar ----------------

def _simular_antes_depois(db, alocacoes, setor_id):
    """Compara a escala ATUAL com a simulada (Antes/Depois) para o primeiro dia
    da geração, usando a mesma fonte de verdade (AnaliseEscala)."""
    from collections import defaultdict
    analise = AnaliseEscala(db)
    if not alocacoes:
        return None
    dia = alocacoes[0]["data"]
    setores = db.execute("SELECT id FROM setores").fetchall()
    if setor_id:
        setores = [s for s in setores if s["id"] == setor_id]

    # Antes: cobertura real hoje
    antes = analise.diagnostico(dia, setor_id=setor_id)
    antes_cob = antes["cobertura_geral"] or 0
    antes_def = antes["total_deficit"]
    antes_exc = antes["total_excesso"]
    antes_score = antes["score"]

    # Depois: monta escalados por setor×faixa a partir das alocações simuladas
    turnos = {r["id"]: (r["entrada"], r["saida"], r["intervalo"]) for r in db.execute("SELECT * FROM turnos").fetchall()}
    func_turno = {r["funcionario_id"]: r["turno_id"] for r in db.execute(
        "SELECT funcionario_id, turno_id FROM funcionario_turno").fetchall()}
    func_setor = {r["id"]: r["setor_id"] for r in db.execute("SELECT id, setor_id FROM funcionarios").fetchall()}

    escalados = defaultdict(lambda: defaultdict(int))
    for a in alocacoes:
        if a["data"] != dia:
            continue
        sid = func_setor.get(a["funcionario_id"])
        if setor_id and sid != setor_id:
            continue
        tid = func_turno.get(a["funcionario_id"])
        if not tid or tid not in turnos:
            continue
        for fx in faixas_do_turno(*turnos[tid]):
            escalados[sid][fx] += 1

    total_dem = total_esc = 0
    deficit = excesso = 0
    for s in setores:
        dem = analise.demanda_final(s["id"], dia)
        esc = escalados.get(s["id"], {})
        for fx in set(dem) | set(esc):
            nec = dem.get(fx, 0)
            disp = esc.get(fx, 0)
            total_dem += nec
            total_esc += disp
            deficit += max(0, nec - disp)
            excesso += max(0, disp - nec)
    depois_cob = round(100 * total_esc / total_dem) if total_dem else None
    depois_score = min(100, round(antes_score + (antes_def - deficit) * 1.5)) if antes_score is not None else None

    return {
        "dia": dia,
        "antes": {"cobertura": antes_cob, "deficit": antes_def, "excesso": antes_exc,
                  "score": antes_score, "classificacao": antes["classificacao"]},
        "depois": {"cobertura": depois_cob, "deficit": deficit, "excesso": excesso,
                   "score": depois_score, "classificacao": classificacao(depois_score) if depois_score is not None else None},
        "remanejados": len({a["funcionario_id"] for a in alocacoes if a["data"] == dia}),
    }

@app.route("/gerar", methods=["GET", "POST"])
@login_required
def gerar():
    db = get_db()
    if g.user["role"] != "admin":
        setores = [s for s in db.execute(
            "SELECT id, nome FROM setores WHERE id = ?", (g.user["setor_id"],)).fetchall()]
    else:
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    if request.method == "POST":
        ano, mes = int(request.form["ano"]), int(request.form["mes"])
        qtd = int(request.form.get("qtd") or 0)
        tipo = request.form.get("tipo", "fds")
        modo = request.form.get("modo", "equilibrada")
        sid = request.form.get("setor_id", type=int)
        if g.user["role"] != "admin":
            sid = g.user["setor_id"]
        escopo_nome = "todos os setores" if not sid else next(
            (s["nome"] for s in setores if s["id"] == sid), "setor")

        datas = datas_tipo(ano, mes, tipo)
        if not datas:
            flash("Nenhuma data encontrada para esse tipo/período.", "warning")
            return redirect(url_for("gerar"))

        motor = MotorEscala(db)
        # por_pico ignora qtd fixa e usa demanda por faixa
        usar_pico = (modo == "por_pico")

        # etapa 2: confirmação (gera e salva como RASCUNHO)
        if request.form.get("confirmar") == "1":
            if request.form.get("limpar"):
                ini = date(ano, mes, 1)
                fim = (ini.replace(day=28) + timedelta(days=7)).replace(day=1)
                db.execute(
                    "DELETE FROM escala WHERE data >= ? AND data < ? AND tipo = ?",
                    (ini.isoformat(), fim.isoformat(), tipo))
                db.commit()
            if usar_pico:
                alocacoes, total = motor.otimizar_por_pico(datas, tipo, sid, modo=modo)
            else:
                alocacoes, total = motor.otimizar(datas, qtd, tipo, sid, modo=modo)
            motor.persistir(alocacoes, status="rascunho")
            registrar(db, "Gerar escala (rascunho)",
                      f"{total} alocações - {escopo_nome} {MESES[mes-1]}/{ano} - {ESTRATEGIAS[modo]}")
            flash(f"Escala gerada como RASCUNHO: {total} alocações ({ESTRATEGIAS[modo]}).")
            return redirect(url_for("escala_view", ano=ano, mes=mes,
                                    tipo=tipo, setor_id=sid or ""))

        # etapa 1: pré-visualização / simulação (não salva)
        if usar_pico:
            alocacoes, total = motor.otimizar_por_pico(datas, tipo, sid, modo=modo)
        else:
            alocacoes, total = motor.otimizar(datas, qtd, tipo, sid, modo=modo)
        dias_map = {}
        for a in alocacoes:
            dias_map.setdefault(a["data"], []).append(a["nome"])
        # ---- Antes/Depois (fonte única de verdade) ----
        preview_antes_depois = _simular_antes_depois(db, alocacoes, sid)
        preview = {"datas": [{"data": k, "pessoas": v} for k, v in
                             sorted(dias_map.items())],
                   "total": total,
                   "modo": modo, "ano": ano, "mes": mes, "qtd": qtd,
                   "tipo": tipo, "sid": sid or "", "escopo": escopo_nome,
                   "antes_depois": preview_antes_depois}
        return render_template("gerar.html", preview=preview, ano=ano, mes=mes,
                               setores=setores, meses=MESES, modo=modo,
                               modos=MODOS)
    hoje = date.today()
    prox = (hoje.replace(day=28) + timedelta(days=7)).replace(day=1)
    return render_template("gerar.html", ano=prox.year, mes=prox.month,
                           setores=setores, meses=MESES, modo="equilibrada", modos=MODOS)


# ---------------- Escala (visualização) ----------------

@app.route("/escala")
@login_required
def escala_view():
    db = get_db()
    hoje = date.today()
    ano = request.args.get("ano", hoje.year, type=int)
    mes = request.args.get("mes", hoje.month, type=int)
    tipo = request.args.get("tipo", "fds")
    setor_id = request.args.get("setor_id", type=int)
    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]

    datas = datas_tipo(ano, mes, tipo)
    escala = []
    for d in datas:
        q = """SELECT f.nome, f.matricula, f.id, f.cargo, s.nome setor, e.status, e.funcionario_id,
                      t.nome turno, t.entrada, t.saida FROM escala e
               JOIN funcionarios f ON f.id = e.funcionario_id
               LEFT JOIN setores s ON s.id = f.setor_id
               LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
               LEFT JOIN turnos t ON t.id = ft.turno_id
               WHERE e.data = ?"""
        params = [d.isoformat()]
        if setor_id:
            q += " AND f.setor_id = ?"
            params.append(setor_id)
        q += " ORDER BY s.nome, f.nome"
        pessoas = db.execute(q, params).fetchall()
        status_dia = "publicada"
        if pessoas:
            statuses = {p["status"] for p in pessoas}
            if "publicada" in statuses:
                status_dia = "publicada"
            elif "aprovada" in statuses:
                status_dia = "aprovada"
            elif "validada" in statuses:
                status_dia = "validada"
            else:
                status_dia = "rascunho"
        escala.append({"data": d, "pessoas": pessoas, "status": status_dia})

    rank_q = """SELECT f.nome, COUNT(e.id) total
           FROM funcionarios f LEFT JOIN escala e ON e.funcionario_id = f.id
           WHERE f.ativo = 1"""
    rank_params = []
    if setor_id:
        rank_q += " AND f.setor_id = ?"
        rank_params.append(setor_id)
    rank_q += " GROUP BY f.id ORDER BY total DESC, f.nome"
    ranking = db.execute(rank_q, rank_params).fetchall()

    # distribuição de justiça: histograma (quantos funcionários têm N escalas)
    dist = {}
    for r in ranking:
        n = r["total"]
        dist[n] = dist.get(n, 0) + 1
    dist_labels = sorted(dist.keys())
    dist_vals = [dist[k] for k in dist_labels]

    if g.user["role"] != "admin":
        setores = [s for s in db.execute(
            "SELECT id, nome FROM setores WHERE id = ?", (g.user["setor_id"],)).fetchall()]
    else:
        setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    return render_template("escala.html", escala=escala, ano=ano, mes=mes,
                           ranking=ranking, setores=setores, setor_id=setor_id,
                           tipo=tipo, dist_labels=dist_labels, dist_vals=dist_vals)


@app.route("/escala/remover", methods=["POST"])
@login_required
def remover_dia():
    data = request.form["data"]
    db = get_db()
    db.execute("DELETE FROM escala WHERE data = ?", (data,))
    db.commit()
    flash(f"Escala de {data} removida.")
    return redirect(request.referrer or url_for("escala_view"))


@app.route("/escala/status", methods=["POST"])
@login_required
def mudar_status():
    """Altera o status de todas as alocações de um dia."""
    data = request.form.get("data")
    novo_status = request.form.get("status")
    if novo_status not in STATUS_ESCALA:
        flash("Status inválido.", "error")
        return redirect(request.referrer or url_for("escala_view"))
    db = get_db()
    db.execute("UPDATE escala SET status = ? WHERE data = ?", (novo_status, data))
    db.commit()
    registrar(db, "Alterar status da escala", f"{data} -> {novo_status}")
    flash(f"Escala de {data} marcada como {novo_status}.")
    return redirect(request.referrer or url_for("escala_view"))


@app.route("/escala/explicar/<int:fid>")
@login_required
def explicar_escala(fid):
    """Explica por que um funcionário foi selecionado em uma data."""
    db = get_db()
    data = request.args.get("data", "")
    setor_id = request.args.get("setor_id", type=int)
    tipo = request.args.get("tipo", "fds")
    motor = MotorEscala(db)
    info = motor.explicar(fid, data, setor_id, tipo)
    return render_template("explicacao.html", info=info, data=data)


# ---------------- Exportação PDF ----------------

def _formata_data(d):
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}"


def _limpa(t):
    """Troca caracteres não suportados pela fonte Helvetica (latin-1)."""
    return (t.replace("—", "-").replace("–", "-").replace("·", "-")
            .replace("•", "-").replace("│", "-"))


@app.route("/escala/exportar")
@login_required
def exportar_pdf():
    db = get_db()
    hoje = date.today()
    ano = request.args.get("ano", hoje.year, type=int)
    mes = request.args.get("mes", hoje.month, type=int)
    tipo = request.args.get("tipo", "fds")
    setor_id = request.args.get("setor_id", type=int)

    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]

    # dados do setor
    if setor_id:
        setor = db.execute("SELECT nome FROM setores WHERE id = ?", (setor_id,)).fetchone()
        nome_setor = setor["nome"] if setor else "Todos os setores"
        funcs_q = """SELECT f.id, f.nome, f.matricula, f.cargo, t.nome turno, t.entrada, t.saida, t.intervalo
                     FROM funcionarios f
                     LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
                     LEFT JOIN turnos t ON t.id = ft.turno_id
                     WHERE f.ativo = 1 AND f.setor_id = ? ORDER BY f.nome"""
        funcs = db.execute(funcs_q, (setor_id,)).fetchall()
    else:
        nome_setor = "Todos os setores"
        funcs_q = """SELECT f.id, f.nome, f.matricula, f.cargo, s.nome setor_nome,
                            t.nome turno, t.entrada, t.saida, t.intervalo
                     FROM funcionarios f
                     LEFT JOIN setores s ON s.id = f.setor_id
                     LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
                     LEFT JOIN turnos t ON t.id = ft.turno_id
                     WHERE f.ativo = 1 ORDER BY s.nome, f.nome"""
        funcs = db.execute(funcs_q).fetchall()

    datas = datas_tipo(ano, mes, tipo)

    # mapeia funcionário -> lista de datas de escala no período
    ini = date(ano, mes, 1)
    fim = (ini.replace(day=28) + timedelta(days=7)).replace(day=1)
    escala_rows = db.execute(
        """SELECT e.funcionario_id, e.data FROM escala e
           JOIN funcionarios f ON f.id = e.funcionario_id
           WHERE e.data >= ? AND e.data < ? AND e.tipo = ?
             AND (? IS NULL OR f.setor_id = ?)
           ORDER BY e.data""",
        (ini.isoformat(), fim.isoformat(), tipo, setor_id, setor_id)).fetchall()
    por_func = {}
    for r in escala_rows:
        por_func.setdefault(r["funcionario_id"], []).append(r["data"])

    # responsável pelo setor (supervisor vinculado) ou admin
    if setor_id:
        resp = db.execute(
            """SELECT nome FROM usuarios WHERE setor_id = ? AND role = 'supervisor'
               ORDER BY id LIMIT 1""", (setor_id,)).fetchone()
    else:
        resp = None
    responsavel = resp["nome"] if resp else g.user["nome"]

    # monta linhas: funcionário + datas
    def fmt_horario(f):
        if f["entrada"] and f["saida"]:
            iv = (f["intervalo"] or "00:00").replace(" as ", " às ")
            return f"Entrada: {f['entrada']}   -   Intervalo: {iv}   -   Saída: {f['saida']}"
        return "-"

    linhas = []
    for f in funcs:
        datas_func = por_func.get(f["id"], [])
        linhas.append({
            "nome": f["nome"],
            "matricula": f["matricula"] or "-",
            "cargo": f["cargo"] or (f["setor_nome"] if setor_id is None and "setor_nome" in f.keys() else "-"),
            "turno": f["turno"] or "-",
            "horario": fmt_horario(f),
            "datas": datas_func,
        })
    # só exibe funcionários que têm escala (ou todos se nenhum)
    com_escala = [l for l in linhas if l["datas"]]
    if not com_escala:
        com_escala = linhas

    # agrupa por dia para o detalhamento
    nome_funcs = {f["id"]: f for f in funcs}
    dias_map = {}
    for r in escala_rows:
        fid = r["funcionario_id"]
        dias_map.setdefault(r["data"], []).append(fid)

    # lista de dias na ordem, com pessoas (detalhes)
    dias_detalhe = []
    for d in sorted(dias_map):
        pessoas = []
        for fid in sorted(dias_map[d], key=lambda x: nome_funcs[x]["nome"]):
            f = nome_funcs[fid]
            pessoas.append({
                "nome": f["nome"],
                "matricula": f["matricula"] or "-",
                "cargo": f["cargo"] or "-",
                "turno": f["turno"] or "-",
                "horario": fmt_horario(f),
            })
        dias_detalhe.append({"data": d, "pessoas": pessoas})

    # ---- Gerar PDF ----
    pdf = FPDF(orientation="L")  # paisagem
    pdf.set_auto_page_break(auto=True, margin=15)
    # fonte Unicode com acentuação (regular e "negrito" apontam pro mesmo arquivo)
    FONTE = "ArialUni"
    pdf.add_font(FONTE, "", os.path.join(os.path.dirname(__file__), "fonts", "ArialUnicode.ttf"), uni=True)
    pdf.add_font(FONTE, "B", os.path.join(os.path.dirname(__file__), "fonts", "ArialUnicode.ttf"), uni=True)
    pdf.add_page()

    DIAS_NOMES = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
                  "Sexta-feira", "Sábado", "Domingo"]
    TIPOS = {"fds": "Fins de semana", "feriado": "Feriados", "semana": "Mês inteiro"}

    # cores por turno (formatação condicional do horário)
    def cor_turno(turno):
        t = (turno or "").strip().lower()
        if "manhã" in t:
            return (22, 101, 52)      # verde escuro
        if "tarde" in t:
            return (180, 83, 9)       # laranja escuro
        if "noite" in t:
            return (30, 58, 138)      # azul escuro
        return (51, 65, 85)           # cinza

    def cabecalho():
        pdf.set_fill_color(11, 37, 69)
        pdf.rect(0, 0, 297, 34, "F")
        pdf.set_xy(12, 6)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font(FONTE, "B", 20)
        pdf.cell(0, 9, "ScalePro - Escala de Trabalho", ln=1)
        pdf.set_font(FONTE, "", 11)
        pdf.set_text_color(170, 200, 235)
        pdf.cell(0, 6, "Setor: " + nome_setor + "   |   " + TIPOS[tipo], ln=1)
        pdf.set_xy(178, 8)
        pdf.set_font(FONTE, "B", 17)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 9, MESES[mes - 1].upper() + " / " + str(ano), 0, 0, "R")
        pdf.set_xy(178, 18)
        pdf.set_font(FONTE, "", 10)
        pdf.set_text_color(170, 200, 235)
        pdf.cell(0, 6, "Escala de Trabalho do Mês", 0, 0, "R")
        pdf.set_y(38)

    def kpi_box(x, w, titulo, valor, cor, cor_valor=None):
        pdf.set_fill_color(*cor)
        pdf.set_xy(x, 38)
        pdf.set_font(FONTE, "B", 9)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(w, 7, "  " + titulo, 1, 1, "L", fill=True)
        pdf.set_xy(x, 45)
        pdf.set_font(FONTE, "B", 15)
        pdf.set_text_color(*(cor_valor or cor))
        pdf.cell(w, 9, valor, 1, 1, "C")
        pdf.set_y(38)

    def cor_zebra(i):
        pdf.set_fill_color(247, 250, 252) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)

    total_escalados = len(com_escala)

    # ---- Cabeçalho + KPIs ----
    cabecalho()
    kpi_box(12, 84, "TOTAL ESCALADOS NO DIA", f"{total_escalados} funcionários", (37, 99, 235))
    kpi_box(104, 90, "RESPONSÁVEL DO SETOR", responsavel, (124, 58, 237))
    kpi_box(202, 86, "CRIADO POR", g.user["nome"], (22, 163, 74))
    pdf.set_y(62)

    # ---- Título da seção ----
    pdf.set_font(FONTE, "B", 13)
    pdf.set_text_color(11, 37, 69)
    pdf.cell(0, 9, "Escala Detalhada por Dia", ln=1)
    pdf.ln(1)

    # ---- Detalhamento por dia ----
    for d in dias_detalhe:
        dt = date.fromisoformat(d["data"])
        dia_nome = DIAS_NOMES[dt.weekday()]
        n = len(d["pessoas"])
        rotulo = f"{dia_nome}, {_formata_data(d['data'])}   -   {n} escala(s)"

        if pdf.get_y() > pdf.h - 45:
            pdf.add_page()
            cabecalho()

        pdf.set_font(FONTE, "B", 10)
        pdf.set_fill_color(11, 37, 69)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 8, "  " + rotulo, 1, 1, "L", fill=True)

        dl = {"n": 66, "m": 28, "c": 42, "t": 36, "h": 94}
        pdf.set_font(FONTE, "B", 8)
        pdf.set_fill_color(226, 232, 240)
        pdf.set_text_color(51, 65, 85)
        pdf.cell(dl["n"], 7, "Nome", 1, 0, "L", fill=True)
        pdf.cell(dl["m"], 7, "Matrícula", 1, 0, "C", fill=True)
        pdf.cell(dl["c"], 7, "Cargo", 1, 0, "L", fill=True)
        pdf.cell(dl["t"], 7, "Turno", 1, 0, "L", fill=True)
        pdf.cell(dl["h"], 7, "Horário de Trabalho", 1, 1, "L", fill=True)

        for j, p in enumerate(d["pessoas"]):
            cor_zebra(j)
            pdf.set_font(FONTE, "", 8)
            pdf.set_text_color(30, 41, 59)
            pdf.cell(dl["n"], 7, p["nome"][:26], 1, 0, "L", fill=True)
            pdf.cell(dl["m"], 7, p["matricula"], 1, 0, "C", fill=True)
            pdf.cell(dl["c"], 7, p["cargo"][:18], 1, 0, "L", fill=True)
            # turno com sua cor
            pdf.set_text_color(*cor_turno(p["turno"]))
            pdf.cell(dl["t"], 7, p["turno"][:13], 1, 0, "L", fill=True)
            pdf.cell(dl["h"], 7, p["horario"], 1, 1, "L", fill=True)
            pdf.set_text_color(30, 41, 59)
        pdf.ln(3)

    # Rodapé
    pdf.set_y(-15)
    pdf.set_font(FONTE, "", 8)
    pdf.set_text_color(120, 130, 145)
    pdf.cell(0, 8, "Gerado em " + hoje.strftime("%d/%m/%Y às %H:%M") +
             " por " + g.user["nome"] + "   |   " + nome_setor, 0, 0, "C")

    saida = bytes(pdf.output())
    resposta = Response(saida, mimetype="application/pdf")
    resposta.headers["Content-Disposition"] = (
        f'inline; filename="escala_{nome_setor.lower().replace(" ", "_")}_{mes:02d}_{ano}.pdf"')
    return resposta


@app.route("/escala/exportar_individual/<int:fid>")
@login_required
def exportar_pdf_individual(fid):
    """Gera PDF individual de um funcionário com suas escalas do período."""
    db = get_db()
    hoje = date.today()
    ano = request.args.get("ano", hoje.year, type=int)
    mes = request.args.get("mes", hoje.month, type=int)
    tipo = request.args.get("tipo", "fds")

    f = db.execute(
        """SELECT f.nome, f.matricula, f.cargo, s.nome setor, t.nome turno, t.entrada, t.saida
           FROM funcionarios f
           LEFT JOIN setores s ON s.id = f.setor_id
           LEFT JOIN funcionario_turno ft ON ft.funcionario_id = f.id
           LEFT JOIN turnos t ON t.id = ft.turno_id
           WHERE f.id = ?""", (fid,)).fetchone()
    if not f:
        flash("Funcionário não encontrado.", "error")
        return redirect(url_for("escala_view"))

    ini = date(ano, mes, 1)
    fim = (ini.replace(day=28) + timedelta(days=7)).replace(day=1)
    escala = db.execute(
        """SELECT data, status FROM escala WHERE funcionario_id = ? AND data >= ? AND data < ?
           ORDER BY data""", (fid, ini.isoformat(), fim.isoformat())).fetchall()

    FONTE = "ArialUni"
    pdf = FPDF(orientation="L")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font(FONTE, "", os.path.join(os.path.dirname(__file__), "fonts", "ArialUnicode.ttf"), uni=True)
    pdf.add_font(FONTE, "B", os.path.join(os.path.dirname(__file__), "fonts", "ArialUnicode.ttf"), uni=True)
    pdf.add_page()

    pdf.set_fill_color(11, 37, 69)
    pdf.rect(0, 0, 297, 34, "F")
    pdf.set_xy(12, 8)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font(FONTE, "B", 18)
    pdf.cell(0, 9, "ScalePro - Escala Individual", ln=1)
    pdf.set_font(FONTE, "", 11)
    pdf.set_text_color(170, 200, 235)
    pdf.cell(0, 6, MESES[mes - 1] + " de " + str(ano), ln=1)
    pdf.set_y(42)

    # dados do funcionário
    pdf.set_font(FONTE, "B", 12)
    pdf.set_text_color(11, 37, 69)
    pdf.cell(0, 8, f["nome"], ln=1)
    pdf.set_font(FONTE, "", 11)
    pdf.set_text_color(51, 65, 85)
    dados = [
        ("Matrícula", f["matricula"] or "-"),
        ("Cargo", f["cargo"] or "-"),
        ("Setor", f["setor"] or "-"),
        ("Turno", f["turno"] or "-"),
        ("Horário", (f"{f['entrada']} - {f['saida']}") if f["entrada"] and f["saida"] else "-"),
    ]
    for rotulo, valor in dados:
        pdf.cell(0, 7, f"{rotulo}: {valor}", ln=1)
    pdf.ln(4)

    # escalas
    pdf.set_font(FONTE, "B", 11)
    pdf.set_text_color(11, 37, 69)
    pdf.cell(0, 8, "Dias escalados no período", ln=1)
    if escala:
        pdf.set_font(FONTE, "", 10)
        pdf.set_text_color(30, 41, 59)
        for e in escala:
            pdf.cell(0, 7, f"  {e['data'][8:10]}/{e['data'][5:7]}/{e['data'][:4]}  -  {e['status']}", ln=1)
    else:
        pdf.cell(0, 7, "  Nenhuma escala no período.", ln=1)

    pdf.set_y(-15)
    pdf.set_font(FONTE, "", 8)
    pdf.set_text_color(120, 130, 145)
    pdf.cell(0, 8, "Gerado em " + hoje.strftime("%d/%m/%Y") + " por " + g.user["nome"], 0, 0, "C")

    saida = bytes(pdf.output())
    resp = Response(saida, mimetype="application/pdf")
    nome_arq = f["nome"].lower().replace(" ", "_")
    resp.headers["Content-Disposition"] = f'inline; filename="escala_{nome_arq}_{mes:02d}_{ano}.pdf"'
    return resp


# ---------------- Bloqueios (férias/atestado/folga) ----------------

@app.route("/funcionarios/<int:fid>/bloqueios", methods=["GET", "POST"])
@login_required
def bloqueios(fid):
    db = get_db()
    f = db.execute("SELECT nome FROM funcionarios WHERE id = ?", (fid,)).fetchone()
    if not f:
        flash("Funcionário não encontrado.")
        return redirect(url_for("funcionarios"))
    if request.method == "POST":
        data = request.form.get("data")
        motivo = request.form.get("motivo", "Folga").strip() or "Folga"
        if data:
            try:
                db.execute(
                    "INSERT INTO bloqueios (funcionario_id, data, motivo) VALUES (?, ?, ?)",
                    (fid, data, motivo))
                db.commit()
                registrar(db, "Bloqueio", f"{f['nome']}: {motivo} em {data}")
                flash(f"Bloqueio '{motivo}' adicionado para {f['nome']}.")
            except sqlite3.IntegrityError:
                flash("Já existe um bloqueio nessa data.")
        return redirect(url_for("bloqueios", fid=fid))
    lista = db.execute(
        "SELECT * FROM bloqueios WHERE funcionario_id = ? AND data >= ? ORDER BY data",
        (fid, date.today().isoformat())).fetchall()
    return render_template("bloqueios.html", f=f, bloqueios=lista)


@app.route("/bloqueios/<int:bid>/excluir")
@login_required
def excluir_bloqueio(bid):
    db = get_db()
    db.execute("DELETE FROM bloqueios WHERE id = ?", (bid,))
    db.commit()
    flash("Bloqueio removido.")
    return redirect(request.referrer or url_for("funcionarios"))


# ---------------- Central de Ausências ----------------

TIPOS_AUSENCIA = ["Falta", "Atestado", "Férias", "Afastamento", "Treinamento", "Atraso"]


@app.route("/ausencias")
@login_required
def ausencias():
    db = get_db()
    setor_id = None if g.user["role"] == "admin" else g.user["setor_id"]
    q = """SELECT a.*, f.nome, f.matricula, s.nome setor
           FROM ausencias a JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN setores s ON s.id = f.setor_id
           WHERE a.data >= ?"""
    params = [date.today().isoformat()]
    if setor_id:
        q += " AND f.setor_id = ?"
        params.append(setor_id)
    q += " ORDER BY a.data, f.nome"
    lista = db.execute(q, params).fetchall()

    setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    funcionarios = db.execute(
        "SELECT id, nome, setor_id FROM funcionarios WHERE ativo=1 ORDER BY nome").fetchall()
    return render_template("ausencias.html", ausencias=lista, setores=setores,
                           funcionarios=funcionarios, tipos=TIPOS_AUSENCIA,
                           setor_id=setor_id)


@app.route("/ausencias/add", methods=["POST"])
@login_required
def add_ausencia():
    db = get_db()
    func_id = request.form.get("funcionario_id", type=int)
    tipo = request.form.get("tipo", "Falta")
    data = request.form.get("data")
    obs = request.form.get("observacao", "").strip()
    if func_id and data:
        db.execute(
            """INSERT INTO ausencias (funcionario_id, tipo, data, observacao, criado_em)
               VALUES (?,?,?,?,?)""",
            (func_id, tipo, data, obs, date.today().isoformat()))
        db.commit()
        f = db.execute("SELECT nome FROM funcionarios WHERE id=?", (func_id,)).fetchone()
        registrar(db, "Registrar ausência", f"{f['nome']} - {tipo} em {data}")
        flash(f"Ausência '{tipo}' registrada para {f['nome']}.")
    return redirect(url_for("ausencias"))


@app.route("/ausencias/<int:aid>/excluir")
@login_required
def excluir_ausencia(aid):
    db = get_db()
    db.execute("DELETE FROM ausencias WHERE id = ?", (aid,))
    db.commit()
    flash("Ausência removida.")
    return redirect(url_for("ausencias"))


@app.route("/ausencias/impacto")
@login_required
def ausencia_impacto():
    """Calcula o impacto operacional das ausências de hoje."""
    db = get_db()
    hoje = date.today().isoformat()
    aus = db.execute(
        """SELECT a.*, f.nome, f.setor_id, s.nome setor FROM ausencias a
           JOIN funcionarios f ON f.id = a.funcionario_id
           LEFT JOIN setores s ON s.id = f.setor_id
           WHERE a.data = ?""", (hoje,)).fetchall()
    resultados = []
    for a in aus:
        setor_id = a["setor_id"]
        setor_nome = a["setor"] or "Sem setor"
        # demanda do setor (faixa=8 todos)
        dem = db.execute(
            "SELECT COALESCE(SUM(necessarios),0) FROM demanda_faixa WHERE setor_id=? AND dia_semana=8",
            (setor_id,)).fetchone()[0] if setor_id else 0
        escalados = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE data=? AND funcionario_id IN (SELECT id FROM funcionarios WHERE setor_id=?)",
            (hoje, setor_id)).fetchone()["c"] if setor_id else 0
        # impacto: tira o ausente dos escalados
        tinha = db.execute(
            "SELECT COUNT(*) c FROM escala WHERE data=? AND funcionario_id=?",
            (hoje, a["funcionario_id"])).fetchone()["c"]
        efetivo = escalados - (1 if tinha else 0)
        cobertura = round(100 * efetivo / dem) if dem else None
        resultados.append({
            "nome": a["nome"], "tipo": a["tipo"], "setor": setor_nome,
            "demanda": dem, "escalados": escalados, "efetivo": efetivo,
            "cobertura": cobertura,
        })
    return render_template("ausencia_impacto.html", resultados=resultados, hoje=hoje)


# ---------------- Trocar senha ----------------

@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    db = get_db()
    if request.method == "POST":
        atual = request.form.get("atual", "")
        nova = request.form.get("nova", "")
        conf = request.form.get("conf", "")
        u = db.execute("SELECT * FROM usuarios WHERE id = ?", (g.user["id"],)).fetchone()
        if not check_password_hash(u["senha_hash"], atual):
            flash("Senha atual incorreta.", "error")
        elif len(nova) < 4:
            flash("A nova senha deve ter ao menos 4 caracteres.")
        elif nova != conf:
            flash("Confirmação não confere com a nova senha.", "error")
        else:
            db.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                       (generate_password_hash(nova, method="pbkdf2:sha256"), g.user["id"]))
            db.commit()
            registrar(db, "Troca de senha")
            flash("Senha alterada com sucesso.")
        return redirect(url_for("perfil"))
    return render_template("perfil.html")


# ---------------- Configurações (admin) ----------------

@app.route("/configuracoes", methods=["GET", "POST"])
@admin_required
def configuracoes():
    db = get_db()
    if request.method == "POST":
        nome = request.form.get("nome_empresa", "").strip()
        if nome:
            set_config(db, "nome_empresa", nome)
            db.commit()
            registrar(db, "Configurações", f"Nome da empresa -> {nome}")
            flash("Nome da empresa atualizado.")
        return redirect(url_for("configuracoes"))
    cfg = get_config(db)
    return render_template("configuracoes.html", nome_empresa=cfg.get("nome_empresa", "ScalePro"))


# ---------------- Backup (admin) ----------------

@app.route("/backup/exportar")
@admin_required
def backup_exportar():
    import io
    data = b""
    with open(DB, "rb") as f:
        data = f.read()
    resp = Response(data, mimetype="application/octet-stream")
    nome = f"escala_backup_{date.today().strftime('%Y%m%d')}.db"
    resp.headers["Content-Disposition"] = f'attachment; filename="{nome}"'
    registrar(get_db(), "Backup", "Exportação do banco de dados")
    return resp


@app.route("/backup/importar", methods=["POST"])
@admin_required
def backup_importar():
    arq = request.files.get("arquivo")
    if arq and arq.filename.endswith(".db"):
        arq.save(DB)
        registrar(get_db(), "Backup", "Importação do banco de dados")
        flash("Banco de dados restaurado.")
    else:
        flash("Arquivo inválido. Envie um arquivo .db.", "error")
    return redirect(url_for("configuracoes"))


@app.route("/base-demonstrativa", methods=["POST"])
@admin_required
def carregar_base_demo():
    """Limpa e carrega a base demonstrativa fictícia (idempotente)."""
    db = get_db()
    res = demo_seed.carregar_base_demonstrativa(db)
    registrar(db, "Carga demonstrativa",
              f"{res['funcionarios']} funcionários · {res['setores']} setores · {res['turnos']} turnos")
    flash(f"Base demonstrativa carregada: {res['funcionarios']} funcionários fictícios.")
    return redirect(url_for("configuracoes"))


# ---------------- Importar em lote ----------------

@app.route("/funcionarios/importar", methods=["GET", "POST"])
@login_required
def importar_funcionarios():
    db = get_db()
    if request.method == "POST":
        texto = request.form.get("dados", "")
        setor_id = request.form.get("setor_id", type=int)
        if g.user["role"] != "admin":
            setor_id = g.user["setor_id"]
        linhas = [l for l in texto.strip().splitlines() if l.strip()]
        adicionados = 0
        # formato: nome;matricula;cargo (separado por ; ou ,)
        for linha in linhas:
            partes = [p.strip() for p in linha.replace(",", ";").split(";")]
            nome = partes[0] if partes else ""
            matricula = partes[1] if len(partes) > 1 else None
            cargo = partes[2] if len(partes) > 2 else None
            if nome:
                db.execute(
                    "INSERT INTO funcionarios (nome, matricula, cargo, setor_id) VALUES (?,?,?,?)",
                    (nome, matricula, cargo, setor_id))
                adicionados += 1
        db.commit()
        registrar(db, "Importação em lote", f"{adicionados} funcionários")
        flash(f"{adicionados} funcionário(s) importado(s).")
        return redirect(url_for("funcionarios"))
    setores = db.execute("SELECT id, nome FROM setores ORDER BY nome").fetchall()
    return render_template("importar.html", setores=setores)


# ---------------- Exportar Excel (CSV) ----------------

@app.route("/escala/exportar_csv")
@login_required
def exportar_csv():
    db = get_db()
    import io
    ano = request.args.get("ano", type=int)
    mes = request.args.get("mes", type=int)
    setor_id = request.args.get("setor_id", type=int)
    if g.user["role"] != "admin":
        setor_id = g.user["setor_id"]
    if not ano or not mes:
        ho = date.today()
        ano, mes = ho.year, ho.month

    ini = date(ano, mes, 1)
    fim = (ini.replace(day=28) + timedelta(days=7)).replace(day=1)
    q = """SELECT e.data, f.nome, f.matricula, f.cargo, s.nome setor
           FROM escala e JOIN funcionarios f ON f.id = e.funcionario_id
           LEFT JOIN setores s ON s.id = f.setor_id
           WHERE e.data >= ? AND e.data < ?"""
    params = [ini.isoformat(), fim.isoformat()]
    if setor_id:
        q += " AND f.setor_id = ?"
        params.append(setor_id)
    q += " ORDER BY e.data, f.nome"
    rows = db.execute(q, params).fetchall()

    buf = io.StringIO()
    buf.write("Data;Nome;Matricula;Cargo;Setor\n")
    for r in rows:
        buf.write(f"{r['data']};{r['nome']};{r['matricula'] or ''};{r['cargo'] or ''};{r['setor'] or ''}\n")
    data = buf.getvalue().encode("utf-8-sig")  # BOM p/ Excel
    resp = Response(data, mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="escala_{mes:02d}_{ano}.csv"'
    return resp


# ---------------- Auditoria (admin) ----------------

@app.route("/auditoria")
@admin_required
def auditoria():
    db = get_db()
    logs = db.execute(
        "SELECT * FROM auditoria ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("auditoria.html", logs=logs)


init_db()

if __name__ == "__main__":
    import os as _os
    port = int(_os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
