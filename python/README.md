# WPM 1 m no Brazil Data Cube

Roda o WPM 1 m no BDC-Lab pelo terminal, sem QGIS: 3 arquivos, 1 configuração, 3 comandos.

## 1. Arquivos

| Arquivo | Onde colocar |
| --- | --- |
| `202608131245_tclt_Ubuntu2404LTS.zip` (TCLT) | Descompactar em `/home/jovyan/tclt` |
| `tclt_runtime_ubuntu2404.tar.xz` | `/home/jovyan/` (não descompactar) |
| `wpm_download.py` | `/home/jovyan/` |

```bash
cd ~
unzip 202608131245_tclt_Ubuntu2404LTS.zip -d ~
ls ~/tclt        # deve mostrar: bin include lib share
```

## 2. Configurar

Abra o `wpm_download.py`, edite só o bloco `CONFIGURATION` no topo e salve (Ctrl+S).

| Variável | Valor |
| --- | --- |
| `ROI_LAT`, `ROI_LON` | Centro da área (graus decimais) |
| `ROI_SIZE_KM` | Lado do quadrado, de 10 a 40 km |
| `ROI_FILE` | Ou um `.gpkg`/`.shp` no lugar do ponto |
| `DATE_START`, `DATE_END` | Período, `AAAA-MM-DD` |
| `PROCESS_ENTIRE_TILE` | `True` = tile inteiro, processado em blocos de `BLOCK_SIZE_KM` (padrão 20 km) e unido num mosaico |
| `OUTPUT_FORMAT` | `"JP2"` ou `"TIFF"` |

`TCLT_HOME` e `OUTPUT_DIR` já vêm certos para o BDC. Se mudar, use sempre caminho completo (`/home/jovyan/...`).

## 3. Rodar

```bash
conda activate geospatial
python ~/wpm_download.py check     # 1ª vez descompacta o pacote; deve terminar em "RESULT: ready"
python ~/wpm_download.py search    # lista as cenas; miniaturas em ~/wpm_tmp/thumbnails
python ~/wpm_download.py process   # digite o número da cena: 1, 1,3 ou 2-4
```

Os produtos ficam em `~/wpm_output` (ex.: `CBERS_4A_WPM_20250712_207_133_L4_RGB_800_8bit.jp2`). Uma cena por tile em cada data; cenas da mesma data viram mosaico.

A pasta `~/wpm_output` recebe só as imagens. No JP2, mantenha o `.aux.xml` junto da imagem: é ele que guarda o nodata (borda transparente no QGIS). Arquivos de trabalho e miniaturas ficam em `~/wpm_tmp` e são apagados ao fim de cada data.

## Lotes grandes

Coloque os IDs das cenas em `SCENE_IDS` e rode em segundo plano:

```bash
nohup python ~/wpm_download.py process > ~/wpm_tmp/run.txt 2>&1 &
tail -f ~/wpm_tmp/run.txt
```

O script detecta CPUs e RAM sozinho e processa várias datas (ou blocos do tile) ao mesmo tempo (ex.: 32 vCPUs e 192 GB → até 4 datas com 8 threads cada; tile inteiro → 2 blocos de 20 km com 16 threads cada). Para limitar: `MAX_CPUS`, `MAX_RAM_GB` e `PARALLEL_JOBS`. Com `CONTINUE_ON_ERROR = True`, as outras datas seguem se uma falhar.

## Erros comuns

| Mensagem | Solução |
| --- | --- |
| `TCLT not found` | `TCLT_HOME = "/home/jovyan/tclt"` (caminho completo) |
| `GLIBC_2.38 ... not found` | Colocar `tclt_runtime_ubuntu2404.tar.xz` em `/home/jovyan` |
| `Only X% of the ROI is covered` | Mover o ponto para dentro da cena, incluir o tile vizinho da mesma data ou usar `PROCESS_ENTIRE_TILE = True` |
| `No scenes found` | Ampliar as datas ou mudar o ponto |
| `not enough tie points` | Escolher outra área ou outra data |
| Só repete o `check` | Faltou `search` ou `process` no fim do comando |
| `Not enough disk in TEMP_DIR for one block` | Diminuir `BLOCK_SIZE_KM` ou apontar `TEMP_DIR` para um disco maior |
| `Block N/M skipped` | Bloco sem feições (ex.: só água); fica vazio no mosaico e o resto do tile é gerado |
