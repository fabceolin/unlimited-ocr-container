---
title: 'Limitar rasterizacao de paginas PDF antes do OCR'
type: 'bugfix'
created: '2026-08-17'
status: 'complete'
baseline_commit: '9f90670bc77d8ade9f2ef054a3cb98896763ac30'
review_loop_iteration: 0
context:
  - 'README.md'
  - 'charts/unlimited-ocr/values.yaml'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** O runner herdado do `baidu/Unlimited-OCR` rasteriza toda pagina a 300 dpi sem considerar suas dimensoes fisicas. Uma pagina de 5669 x 6803 pontos virou um PNG de 669.641.181 pixels; o Pillow recusou a imagem e o SGLang repetiu o payload base64 nos tracebacks ate o Job consumir 16,3 GiB de RAM e sofrer `OOMKilled`.

**Approach:** Tratar o PDF no container, antes da codificacao base64 e da chamada ao SGLang. Um adaptador local preservara o `infer.py` pinado da Baidu, calculara o tamanho previsto por pagina e reduzira somente o DPI das paginas que ultrapassarem um teto configuravel, mantendo proporcao e paginas normais inalteradas.

## Boundaries & Constraints

**Always:** Aplicar o limite por pagina nos caminhos PDF GPU e CPU; usar 25.000.000 pixels como default configuravel por `OCR_MAX_PAGE_PIXELS`; garantir por arredondamento que o raster final nao ultrapasse o teto; registrar dimensoes/DPI solicitados e efetivos quando houver reducao; propagar a configuracao em Jobs diretos e nos Jobs criados pela API; preservar `image_mode=base`, `concurrency=1`, `libnuma1`, a API assincrona e a preservacao de evidencia em falhas.

**Ask First:** Rejeitar o PDF em vez de reduzi-lo; mudar prompt, modo de imagem, concorrencia, modelo, wheel SGLang ou commit Baidu; adicionar normalizacao destrutiva do PDF original.

**Never:** Desabilitar globalmente a protecao `PIL.Image.MAX_IMAGE_PIXELS`; capturar `DecompressionBombError` depois que a imagem gigante ja foi criada; repetir erro deterministico; registrar ou interpolar conteudo base64; alterar diretamente o checkout Baidu baixado durante o build.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Pagina comum | A4 a 300 dpi, ~8,7 MP | Rasteriza a 300 dpi sem mudanca | N/A |
| Pagina do incidente | 5669 x 6803 pt, previsao de 669,6 MP | Reduz DPI mantendo proporcao e produz no maximo 25 MP | Emite uma linha curta de diagnostico, sem base64 |
| PDF misto | Paginas abaixo e acima do teto | Calcula DPI independentemente por pagina | Uma pagina patologica nao reduz as demais |
| Configuracao customizada | Inteiro positivo em `OCR_MAX_PAGE_PIXELS` | Usa o teto informado nos runners CPU/GPU | Valor vazio usa default; invalido falha com mensagem clara antes da requisicao SGLang |
| PDF invalido | Documento ilegivel ou pagina com geometria nao finita/positiva | Nao envia imagem ao modelo | Job falha claramente, sem retry do payload |

</frozen-after-approval>

## Code Map

- `Dockerfile` -- incorpora o adaptador compartilhado nos targets GPU e CPU sem mudar o pin Baidu.
- `scripts/pdf_raster.py` -- novo calculo puro do plano de rasterizacao e conversao PDF limitada.
- `scripts/gpu_infer.py` / `scripts/entrypoint.sh` -- adaptam apenas `pdf_to_images` do upstream antes de executar seu `main`.
- `scripts/cpu_infer.py` -- reutiliza o mesmo rasterizador limitado.
- `api/app.py` -- repassa o teto ao container do Job criado por requisicao.
- `charts/unlimited-ocr/{values.yaml,templates/job.yaml,templates/api-deployment.yaml}` -- expoem e propagam a configuracao.
- `tests/test_pdf_raster.py` -- cobre formula, arredondamento, PDF misto, configuracao e raster real reduzido.
- `README.md` / `charts/unlimited-ocr/README.md` -- documentam default, override e diagnostico.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/pdf_raster.py` -- implementar planejamento e rasterizacao limitada, independentes do modelo.
- [x] `scripts/gpu_infer.py`, `scripts/entrypoint.sh`, `scripts/cpu_infer.py`, `Dockerfile` -- integrar ambos os runners sem copiar nem editar `infer.py` da Baidu.
- [x] `api/app.py`, `charts/unlimited-ocr/values.yaml`, `charts/unlimited-ocr/templates/*.yaml` -- transportar `OCR_MAX_PAGE_PIXELS` ate cada Job.
- [x] `tests/test_pdf_raster.py` -- validar os cenarios da matriz com testes unitarios e um PDF sintetico pequeno.
- [x] `README.md`, `charts/unlimited-ocr/README.md` -- registrar comportamento operacional e configuracao.

**Acceptance Criteria:**
- Given a pagina do incidente e teto default, when o plano de rasterizacao e calculado, then o produto largura x altura final e no maximo 25.000.000 e o DPI efetivo e menor que 300.
- Given uma pagina comum abaixo do teto, when rasterizada, then dimensoes e DPI permanecem equivalentes ao upstream.
- Given o build GPU, when iniciado, then ele ainda executa o mesmo `infer.py`/wheel Baidu pinado, substituindo apenas a funcao de rasterizacao PDF.
- Given um Job Helm direto ou criado pela API, when renderizado, then recebe exatamente o teto configurado.

## Spec Change Log

- `2026-08-17` -- Review reforcou a fronteira de entrada: documentos nao-PDF e PDFs sem paginas agora falham antes da criacao de raster; diagnosticos de DPI muito pequeno preservam algarismos significativos.

## Design Notes

O teto controla memoria antes da descompressao: `pixels = ceil(largura_pt * dpi / 72) * ceil(altura_pt * dpi / 72)`. Se excedido, o DPI e multiplicado inicialmente por `sqrt(teto / pixels)` e ajustado para baixo ate que o produto com arredondamento caiba. O default de 25 MP preserva A4/Letter a 300 dpi e limita um raster RGB cru a aproximadamente 72 MiB, antes das estruturas do modelo.

## Verification

**Commands:**
- `python -m unittest discover -s tests -v` -- todos os cenarios passam sem carregar modelo/GPU.
- `python -m compileall scripts api` -- modulos novos e alterados compilam.
- `helm lint charts/unlimited-ocr && helm template test charts/unlimited-ocr` -- chart valido e env renderizado.
- `git diff --check` -- sem erros de whitespace.

**Review disposition:** corrigidos os casos nao-PDF, zero paginas e precisao do log. Mantido o lifecycle temporario do upstream porque cada execucao ocorre em um container efemero; retry HTTP generico e limite agregado multipagina nao pertencem ao guard de pixels por pagina e nao foram alterados.
