import os
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pypdf import PdfReader, PdfWriter

try:
    from PIL import Image, ImageTk
    TEM_PIL = True
except ImportError:
    TEM_PIL = False


def obter_caminho_recurso(nome_arquivo: str) -> str:
    """Resolve o caminho de recursos (como arruda.ico) tanto no VS Code quanto dentro do .exe."""
    if hasattr(sys, "_MEIPASS"):
        caminho_meipass = os.path.join(sys._MEIPASS, nome_arquivo)
        if os.path.exists(caminho_meipass):
            return caminho_meipass
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, nome_arquivo)


def extrair_linhas_visuais(pagina) -> list[str]:
    """
    Reconstrói as linhas exatamente na ordem visual (de cima para baixo, da esquerda para a direita)
    usando as coordenadas X/Y do PDF, evitando que colunas do Domínio fiquem embaralhadas.
    """
    fragmentos = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text and text.strip():
            x = float(tm[4])
            y = float(tm[5])
            fragmentos.append((y, x, text.strip()))

    pagina.extract_text(visitor_text=visitor)

    # Ordena de cima para baixo (Y decrescente) e da esquerda para a direita (X crescente)
    fragmentos.sort(key=lambda item: (-round(item[0] / 4.0), item[1]))

    linhas = []
    y_atual = None
    linha_atual = []

    for y, x, txt in fragmentos:
        y_bucket = round(y / 4.0)
        if y_atual is None or y_bucket == y_atual:
            linha_atual.append(txt)
            y_atual = y_bucket
        else:
            linhas.append(" ".join(linha_atual))
            linha_atual = [txt]
            y_atual = y_bucket

    if linha_atual:
        linhas.append(" ".join(linha_atual))

    return linhas


def extrair_pares_relacao(caminho_pdf: str) -> list[dict]:
    """
    Lê cada linha visual da Relação de Recebimentos e extrai:
    - 'boleto' (coluna Boleto)
    - 'vencimento' (coluna Vencimento)
    - 'cliente' (código + início do nome)
    """
    reader = PdfReader(caminho_pdf)
    titulos = []
    vistos = set()
    ultimo_cliente = "Cliente"

    for pagina in reader.pages:
        linhas = extrair_linhas_visuais(pagina)
        texto_pagina = pagina.extract_text() or ""

        for linha in linhas:
            linha_up = linha.upper()

            # Ignora cabeçalhos, rodapés e linha de totais
            if any(
                ign in linha_up
                for ign in [
                    "ORGANIZACAO CONTABIL ARRUDA",
                    "RELAÇÃO DE RECEBIMENTOS",
                    "RELACAO DE RECEBIMENTOS",
                    "CLIENTE NOME",
                    "SISTEMA LICENCIADO PARA",
                    "TOTAIS:",
                    "RECEBIMENTO:",
                ]
            ):
                continue

            # Remove CNPJ e CPFs de 11 dígitos colados no nome de MEI
            linha_limpa = re.sub(r"00\.000\.000/0000-00", " ", linha)
            linha_limpa = re.sub(r"\b\d{11}\b", " ", linha_limpa)

            # Procura número do boleto (6 a 10 dígitos) na linha visual
            m_boleto = re.search(r"(?<![\d/\.,-])\b(\d{6,10})\b(?![\d/\.,-])", linha_limpa)
            if not m_boleto:
                continue

            trecho_antes_boleto = linha_limpa[:m_boleto.start()]

            # O código do cliente aparece imediatamente antes da primeira data.
            # Isso funciona também nas linhas que começam com "1 4083 data...".
            m_codigo = re.search(
                r"(?:^|\s)(\d{1,5})\s+(?=\d{2}/\d{2}/20\d{2})",
                trecho_antes_boleto,
            )

            # A última sequência alfabética antes do boleto contém o nome do cliente.
            blocos_nome = re.findall(
                r"[A-ZÀ-Ú][A-ZÀ-Ú0-9\s\.\-\&\+\/\(\)']{2,}",
                trecho_antes_boleto,
            )
            nome_completo = ""
            if blocos_nome:
                nome_completo = max(
                    (bloco.strip() for bloco in blocos_nome),
                    key=len,
                )
                nome_completo = re.sub(r"\s+", " ", nome_completo).strip()

            if m_codigo and nome_completo:
                ultimo_cliente = f"{m_codigo.group(1)} - {nome_completo}"

            num_boleto = m_boleto.group(1).lstrip("0")
            if not num_boleto:
                continue

            # Na reconstrução visual, o vencimento pode aparecer antes do boleto
            # e a data de recebimento depois. Por isso usamos a primeira data
            # completa da linha como vencimento, não a primeira data após o boleto.
            # Nas linhas visuais a data pode ficar colada à parcela/competência
            # (por exemplo 20/11/20261/110/2026), por isso os limites não usam
            # word-boundary.
            datas = re.findall(r"(\d{2}/\d{2}/20\d{2})", linha_limpa)
            if not datas:
                continue

            vencimento = datas[0]
            chave = (num_boleto, vencimento)

            if chave not in vistos:
                vistos.add(chave)
                titulos.append({
                    "boleto": num_boleto,
                    "vencimento": vencimento,
                    "cliente": ultimo_cliente,
                })

        # Segunda leitura: algumas versões do PDF embaralham as colunas na
        # reconstrução visual. Este padrão procura diretamente a sequência
        # [boleto] [parcela] [vencimento], que é a estrutura desta relação.
        texto_limpo = re.sub(r"[ \t]+", " ", texto_pagina)
        texto_limpo = re.sub(r"\s*\n\s*", " ", texto_limpo)
        for m in re.finditer(
            r"\b(?<!/)(\d{6,10})\b\s+[0-9]+/[0-9]+\s+"
            r"(\d{2}/\d{2}/20\d{2})\b",
            texto_limpo,
        ):
            num_boleto = m.group(1).lstrip("0")
            vencimento = m.group(2)
            if not num_boleto:
                continue
            chave = (num_boleto, vencimento)
            if chave not in vistos:
                vistos.add(chave)
                titulos.append({
                    "boleto": num_boleto,
                    "vencimento": vencimento,
                    "cliente": "Cliente não identificado na leitura alternativa",
                })

    return titulos


def extrair_dados_boleto(caminho_pdf: str) -> list[dict]:
    """
    Lê cada página de um PDF de boleto e extrai:
    - 'nosso_numero' (campo Nosso número / linha digitável)
    - 'vencimento' (campo Vencimento)
    """
    paginas = []
    try:
        reader = PdfReader(caminho_pdf)
        for idx_pag, pagina in enumerate(reader.pages):
            texto = pagina.extract_text() or ""
            if not texto.strip():
                continue

            if "RELAÇÃO DE RECEBIMENTOS" in texto.upper() or "RELACAO DE RECEBIMENTOS" in texto.upper():
                continue

            nosso_num = None
            texto_continuo = re.sub(r"\s+", "", texto)

            # 1. Extrai diretamente da linha digitável padrão Thomson Reuters (509-6).
            # O segmento visual pode variar em comprimento ou não ter o tamanho exato
            # entre o código de barras e o valor, portanto testamos todas as formas
            # conhecidas antes de usar o campo textual.
            m_linha = re.search(r"5099\d{19}(\d{8})\d{16}", texto_continuo)
            if m_linha:
                nosso_num = m_linha.group(1).lstrip("0")

            if not nosso_num:
                m_linha = re.search(r"5099\d+?(\d{8})(?:\d{10,16})", texto_continuo)
                if m_linha:
                    nosso_num = m_linha.group(1).lstrip("0")

            # 2. Fallback pelo campo 'Nosso número'.
            if not nosso_num:
                texto_sem_benef = texto.replace("000000000", " ")
                m_nosso = re.search(
                    r"Nosso\s+n[uú]mero.{0,60}?\b(\d{6,10})\b",
                    texto_sem_benef,
                    re.IGNORECASE | re.DOTALL,
                )
                if m_nosso:
                    nosso_num = m_nosso.group(1).lstrip("0")

            # 3. Alguns PDFs preservam o campo em uma linha separada com
            # quebras. Neste caso, procura o primeiro número de 6 a 10 dígitos
            # imediatamente depois da legenda.
            if not nosso_num:
                for m_nosso in re.finditer(
                    r"Nosso\s+n[uú]mero",
                    texto,
                    re.IGNORECASE,
                ):
                    trecho = texto[m_nosso.end():m_nosso.end() + 160]
                    numeros = re.findall(r"\b\d{6,10}\b", trecho)
                    for numero in numeros:
                        if numero not in {"411320591"}:
                            nosso_num = numero.lstrip("0")
                            break
                    if nosso_num:
                        break

            if not nosso_num:
                continue

            # 4. Extrai o vencimento real do boleto.
            vencimento = None
            m_inst = re.search(r"ap[oó]s\s+(\d{2}/\d{2}/20\d{2})", texto, re.IGNORECASE)
            if m_inst:
                vencimento = m_inst.group(1)

            if not vencimento:
                for m_venc in re.finditer(
                    r"Vencimento\b(.{0,140}?)(\d{2}/\d{2}/20\d{2})",
                    texto,
                    re.IGNORECASE | re.DOTALL,
                ):
                    if not re.search(r"Data\s+do\s+(?:Documento|Processamento)", m_venc.group(1), re.IGNORECASE):
                        vencimento = m_venc.group(2)
                        break

            if nosso_num and vencimento:
                paginas.append({
                    "caminho": caminho_pdf,
                    "arquivo": os.path.basename(caminho_pdf),
                    "pagina": idx_pag,
                    "nosso_numero": nosso_num,
                    "vencimento": vencimento,
                })
    except Exception:
        pass

    return paginas


class AppConciliacaoBoletos:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Unificador de Boletos - Arruda Empresarial")
        self.root.geometry("850x715")
        self.root.configure(bg="#F4F6F9")
        self.root.resizable(False, False)

        self.logo_img = None
        self._carregar_icone_janela()

        self.caminho_relacao = tk.StringVar()
        self.arquivos_boletos: list[str] = []
        self.texto_progresso = tk.StringVar(value="Aguardando início do processamento...")

        self._configurar_estilos()
        self._montar_interface()

    def _carregar_icone_janela(self):
        caminho_ico = obter_caminho_recurso("arruda.ico")
        if os.path.exists(caminho_ico):
            try:
                self.root.iconbitmap(caminho_ico)
            except Exception:
                pass
            if TEM_PIL:
                try:
                    img = Image.open(caminho_ico).convert("RGBA")
                    img = img.resize((48, 48), Image.Resampling.LANCZOS)
                    self.logo_img = ImageTk.PhotoImage(img)
                except Exception:
                    self.logo_img = None

    def _configurar_estilos(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Arruda.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background="#16A34A",
            bordercolor="#CBD5E1",
            lightcolor="#22C55E",
            darkcolor="#15803D",
            thickness=18,
        )

    def _montar_interface(self):
        header = tk.Frame(self.root, bg="#1B365D", height=74)
        header.pack(fill="x")
        header.pack_propagate(False)

        header_inner = tk.Frame(header, bg="#1B365D")
        header_inner.pack(expand=True)

        if self.logo_img:
            lbl_logo = tk.Label(header_inner, image=self.logo_img, bg="#1B365D")
            lbl_logo.pack(side="left", padx=(0, 14))

        frame_titulos = tk.Frame(header_inner, bg="#1B365D")
        frame_titulos.pack(side="left")

        tk.Label(
            frame_titulos,
            text="ARRUDA EMPRESARIAL — UNIFICADOR DE BOLETOS",
            font=("Segoe UI", 13, "bold"),
            fg="white",
            bg="#1B365D",
        ).pack(anchor="w")
        tk.Label(
            frame_titulos,
            text="Junta somente os PDFs que baterem Nosso Número + Vencimento com a Relação",
            font=("Segoe UI", 9),
            fg="#CBD5E1",
            bg="#1B365D",
        ).pack(anchor="w")

        container = tk.Frame(self.root, bg="#F4F6F9", padx=22, pady=14)
        container.pack(fill="both", expand=True)

        # Etapa 1
        tk.Label(
            container,
            text="1. Selecione o PDF da Relação de Recebimentos:",
            font=("Segoe UI", 10, "bold"),
            bg="#F4F6F9",
            fg="#1E293B",
        ).pack(anchor="w")

        frame_rel = tk.Frame(container, bg="#F4F6F9")
        frame_rel.pack(fill="x", pady=(4, 12))

        tk.Entry(
            frame_rel,
            textvariable=self.caminho_relacao,
            font=("Segoe UI", 9),
            state="readonly",
            readonlybackground="white",
        ).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))

        tk.Button(
            frame_rel,
            text="Selecionar Relação (PDF)",
            font=("Segoe UI", 9, "bold"),
            bg="#2563EB",
            fg="white",
            relief="flat",
            padx=14,
            cursor="hand2",
            command=self._selecionar_relacao,
        ).pack(side="right", ipady=3)

        # Etapa 2
        header_lista = tk.Frame(container, bg="#F4F6F9")
        header_lista.pack(fill="x")

        self.lbl_qtd_arquivos = tk.Label(
            header_lista,
            text="2. PDFs de Boletos Carregados (0 arquivos):",
            font=("Segoe UI", 10, "bold"),
            bg="#F4F6F9",
            fg="#1E293B",
        )
        self.lbl_qtd_arquivos.pack(side="left")

        frame_botoes_arq = tk.Frame(container, bg="#F4F6F9")
        frame_botoes_arq.pack(fill="x", pady=(4, 6))

        tk.Button(
            frame_botoes_arq,
            text="+ Adicionar Vários PDFs (Ctrl+A)",
            font=("Segoe UI", 9, "bold"),
            bg="#2563EB",
            fg="white",
            relief="flat",
            padx=12,
            cursor="hand2",
            command=self._adicionar_pdfs,
        ).pack(side="left", ipady=3, padx=(0, 8))

        tk.Button(
            frame_botoes_arq,
            text="+ Adicionar Pasta Inteira (c/ Subpastas)",
            font=("Segoe UI", 9, "bold"),
            bg="#0284C7",
            fg="white",
            relief="flat",
            padx=12,
            cursor="hand2",
            command=self._adicionar_pasta,
        ).pack(side="left", ipady=3, padx=(0, 8))

        tk.Button(
            frame_botoes_arq,
            text="Limpar Lista",
            font=("Segoe UI", 9),
            bg="#E2E8F0",
            fg="#334155",
            relief="flat",
            padx=10,
            cursor="hand2",
            command=self._limpar_lista,
        ).pack(side="right", ipady=3)

        frame_listbox = tk.Frame(container, bg="white", bd=1, relief="solid")
        frame_listbox.pack(fill="x", pady=(0, 12))

        scroll_list = tk.Scrollbar(frame_listbox)
        scroll_list.pack(side="right", fill="y")

        self.listbox_arquivos = tk.Listbox(
            frame_listbox,
            height=6,
            font=("Segoe UI", 8),
            bg="white",
            fg="#1E293B",
            borderwidth=0,
            highlightthickness=0,
            yscrollcommand=scroll_list.set,
        )
        self.listbox_arquivos.pack(side="left", fill="both", expand=True, padx=6, pady=4)
        scroll_list.config(command=self.listbox_arquivos.yview)

        # Botão Principal
        self.btn_executar = tk.Button(
            container,
            text="CONFERIR (BOLETO + VENCIMENTO) E JUNTAR PDFs BATIDOS",
            font=("Segoe UI", 10, "bold"),
            bg="#16A34A",
            fg="white",
            relief="flat",
            cursor="hand2",
            command=self._iniciar_processamento,
        )
        self.btn_executar.pack(fill="x", ipady=8, pady=(0, 12))

        # Barra de Progresso
        frame_prog_header = tk.Frame(container, bg="#F4F6F9")
        frame_prog_header.pack(fill="x")

        tk.Label(
            frame_prog_header,
            text="Nível de Processamento:",
            font=("Segoe UI", 9, "bold"),
            bg="#F4F6F9",
            fg="#1E293B",
        ).pack(side="left")

        tk.Label(
            frame_prog_header,
            textvariable=self.texto_progresso,
            font=("Segoe UI", 9, "bold"),
            bg="#F4F6F9",
            fg="#1B365D",
        ).pack(side="right")

        self.progress_bar = ttk.Progressbar(
            container,
            style="Arruda.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress_bar.pack(fill="x", pady=(4, 12))

        # Log
        tk.Label(
            container,
            text="Status e Log de Conferência:",
            font=("Segoe UI", 9, "bold"),
            bg="#F4F6F9",
            fg="#475569",
        ).pack(anchor="w")

        self.txt_log = scrolledtext.ScrolledText(
            container,
            height=13,
            font=("Consolas", 9),
            bg="#0F172A",
            fg="#E2E8F0",
            relief="flat",
            padx=10,
            pady=8,
        )
        self.txt_log.pack(fill="both", expand=True, pady=(4, 0))

    def _atualizar_progresso(self, percentual: float, descricao: str):
        self.progress_bar["value"] = percentual
        self.texto_progresso.set(f"{int(percentual)}% — {descricao}")
        self.root.update_idletasks()

    def _atualizar_lista_visual(self):
        self.listbox_arquivos.delete(0, tk.END)
        for arq in self.arquivos_boletos:
            self.listbox_arquivos.insert(tk.END, f"  {os.path.basename(arq)}   ({os.path.dirname(arq)})")
        self.lbl_qtd_arquivos.config(
            text=f"2. PDFs de Boletos Carregados ({len(self.arquivos_boletos)} arquivos):"
        )

    def _adicionar_pdfs(self):
        arquivos = filedialog.askopenfilenames(
            title="Selecione um ou vários PDFs de Boletos",
            filetypes=[("Arquivos PDF", "*.pdf")],
        )
        if arquivos:
            existentes = set(self.arquivos_boletos)
            rel_abs = os.path.abspath(self.caminho_relacao.get()) if self.caminho_relacao.get() else ""
            for arq in arquivos:
                arq_abs = os.path.abspath(arq)
                if arq_abs not in existentes and arq_abs != rel_abs:
                    self.arquivos_boletos.append(arq_abs)
                    existentes.add(arq_abs)
            self._atualizar_lista_visual()

    def _adicionar_pasta(self):
        pasta = filedialog.askdirectory(title="Selecione a Pasta dos Boletos")
        if pasta:
            existentes = set(self.arquivos_boletos)
            rel_abs = os.path.abspath(self.caminho_relacao.get()) if self.caminho_relacao.get() else ""
            for raiz, _, arquivos in os.walk(pasta):
                for f in arquivos:
                    if f.lower().endswith(".pdf"):
                        arq_abs = os.path.abspath(os.path.join(raiz, f))
                        if arq_abs not in existentes and arq_abs != rel_abs:
                            self.arquivos_boletos.append(arq_abs)
                            existentes.add(arq_abs)
            self._atualizar_lista_visual()

    def _limpar_lista(self):
        self.arquivos_boletos.clear()
        self._atualizar_lista_visual()
        self._atualizar_progresso(0, "Lista limpa. Aguardando arquivos...")

    def _log(self, mensagem: str):
        self.txt_log.insert(tk.END, mensagem + "\n")
        self.txt_log.see(tk.END)
        self.root.update_idletasks()

    def _selecionar_relacao(self):
        arquivo = filedialog.askopenfilename(
            title="Selecione a Relação de Recebimentos",
            filetypes=[("Arquivos PDF", "*.pdf")],
        )
        if arquivo:
            self.caminho_relacao.set(arquivo)

    def _iniciar_processamento(self):
        relacao = self.caminho_relacao.get()

        if not relacao or not os.path.isfile(relacao):
            messagebox.showwarning("Atenção", "Selecione o arquivo PDF da Relação de Recebimentos.")
            return
        if not self.arquivos_boletos:
            messagebox.showwarning("Atenção", "Adicione pelo menos um PDF de boleto na lista.")
            return

        arquivo_saida = filedialog.asksaveasfilename(
            title="Salvar PDF Unificado Como",
            defaultextension=".pdf",
            initialfile="Relacao_e_Boletos_Unificados.pdf",
            filetypes=[("Arquivo PDF", "*.pdf")],
        )
        if not arquivo_saida:
            return

        self.btn_executar.config(state="disabled", text="PROCESSANDO...")
        self.txt_log.delete("1.0", tk.END)
        self._atualizar_progresso(2, "Lendo Relação de Recebimentos...")

        threading.Thread(
            target=self._processar,
            args=(relacao, list(self.arquivos_boletos), arquivo_saida),
            daemon=True,
        ).start()

    def _processar(self, relacao_path: str, lista_pdfs: list[str], saida_path: str):
        try:
            self._log(">>> Lendo colunas [Boleto] e [Vencimento] na Relação de Recebimentos...")
            lista_relacao = extrair_pares_relacao(relacao_path)
            self._log(f"    Encontrados {len(lista_relacao)} títulos na relação.")
            if lista_relacao:
                ex_r = lista_relacao[0]
                self._log(f"    Exemplo lido da Relação: Boleto {ex_r['boleto']} | Venc {ex_r['vencimento']}\n")
            self._atualizar_progresso(10, f"Relação lida ({len(lista_relacao)} títulos). Lendo boletos...")

            if not lista_relacao:
                self._log("[ERRO] Nenhum título foi identificado na Relação de Recebimentos.")
                self._atualizar_progresso(0, "Erro: nenhum título encontrado.")
                return

            self._log(f">>> Lendo [Nosso número] e [Vencimento] nos {len(lista_pdfs)} PDFs...")
            mapa_boletos: dict[tuple[str, str], dict] = {}
            total_pdfs = len(lista_pdfs)

            for idx, pdf in enumerate(lista_pdfs, start=1):
                for pag_info in extrair_dados_boleto(pdf):
                    chave = (pag_info["nosso_numero"], pag_info["vencimento"])
                    if chave not in mapa_boletos:
                        mapa_boletos[chave] = pag_info

                pct = 10 + (idx / total_pdfs) * 55
                self._atualizar_progresso(pct, f"Lendo PDFs ({idx}/{total_pdfs}): {os.path.basename(pdf)}")

            self._log(f"    Total de {len(mapa_boletos)} boleto(s) únicos indexados.")
            if mapa_boletos:
                ex_b = next(iter(mapa_boletos.values()))
                self._log(f"    Exemplo lido dos PDFs: Nosso nº {ex_b['nosso_numero']} | Venc {ex_b['vencimento']}\n")

            writer = PdfWriter()
            writer.append(relacao_path)
            self._log("[ABERTURA] Relação de Recebimentos inserida na abertura do PDF.\n")

            encontrados = 0
            nao_encontrados = []
            chaves_juntadas = set()
            total_titulos = len(lista_relacao)

            self._log(">>> Batendo (Boleto + Vencimento) x (Nosso número + Vencimento)...\n")

            for idx, item in enumerate(lista_relacao, start=1):
                chave = (item["boleto"], item["vencimento"])

                if chave in mapa_boletos and chave not in chaves_juntadas:
                    pag_info = mapa_boletos[chave]
                    writer.append(pag_info["caminho"], pages=[pag_info["pagina"]])
                    chaves_juntadas.add(chave)
                    encontrados += 1
                    self._log(
                        f"[JUNTADO] {item['cliente']} | Boleto {item['boleto']} | Venc {item['vencimento']} -> {pag_info['arquivo']}"
                    )
                else:
                    nao_encontrados.append(
                        f"Boleto {item['boleto']} - {item['cliente']} (Venc: {item['vencimento']})"
                    )

                pct = 65 + (idx / total_titulos) * 30
                self._atualizar_progresso(pct, f"Conferindo {idx}/{total_titulos}...")

            if encontrados == 0:
                self._log("\n" + "=" * 68)
                self._log("[AVISO] Nenhum boleto bateu (Nosso número + Vencimento) com a relação.")
                self._atualizar_progresso(0, "Concluído sem correspondências.")
                messagebox.showwarning(
                    "Nenhuma correspondência",
                    "Nenhum boleto bateu simultaneamente o Nosso número e o Vencimento com a relação.",
                )
                return

            self._atualizar_progresso(97, "Salvando arquivo PDF final...")
            with open(saida_path, "wb") as f_out:
                writer.write(f_out)

            self._atualizar_progresso(100, f"Concluído! {encontrados} de {total_titulos} boletos juntados.")

            self._log("\n" + "=" * 68)
            self._log(f"CONCLUÍDO! PDF salvo em: {saida_path}")
            self._log(f"Boletos na Relação: {total_titulos} | Boletos Juntados: {encontrados}")

            if nao_encontrados:
                self._log(
                    f"\n[AVISO] {len(nao_encontrados)} boleto(s) da relação não estavam nos PDFs selecionados:"
                )
                for item_faltante in nao_encontrados:
                    self._log(f"  - {item_faltante}")

            messagebox.showinfo(
                "Processo Concluído",
                f"PDF gerado com sucesso!\n\n"
                f"• Relação de Recebimentos na abertura\n"
                f"• Boletos juntados (Boleto + Vencimento batidos): {encontrados} de {total_titulos}\n"
                f"• Boletos da relação sem PDF correspondente: {len(nao_encontrados)}",
            )

        except Exception as e:
            self._log(f"\n[ERRO CRÍTICO] {str(e)}")
            self._atualizar_progresso(0, "Erro durante o processamento.")
            messagebox.showerror("Erro", f"Ocorreu um erro durante o processamento:\n{e}")
        finally:
            self.btn_executar.config(
                state="normal",
                text="CONFERIR (BOLETO + VENCIMENTO) E JUNTAR PDFs BATIDOS",
            )


if __name__ == "__main__":
    root = tk.Tk()
    app = AppConciliacaoBoletos(root)
    root.mainloop()