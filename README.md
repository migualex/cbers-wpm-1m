# WPM 1-meter spatial resolution
<p align="center">
  <img src="https://img.shields.io/badge/License-GPLv3-blue" />
  <img src="https://img.shields.io/badge/Lifecycle-maturing-green.svg" />
  <img src="https://img.shields.io/badge/Python-3.11.9-3776AB?logo=python&logoColor=white" />
</p>

<img src="icons/icon.png" alt="wpm" align="right" height="200" width="200"/>

<p>O método de registro, restauração e fusão implementado por Laércio Massaru Namikawa foi uma adaptação à metodologia de restauração de Fonseca et al. (1993), utilizando o software TerraLib Command-Line Tools (TCLT), de Emiliano Ferreira Castejon.</p>

<p>O plugin é uma ferramenta destinada à geração de imagens do satélite CBERS-4A/WPM com resolução espacial de 1 metro – cuja fusão original possui 2 metros de resolução – a partir do SpatioTemporal Asset Catalog (STAC) do projeto Brazil Data Cube (INPE).</p>

## Instalação das dependências

Leia as instruções completas em [MANUAL.md](MANUAL.md). O plugin foi desenvolvido para a versão 3.44 do QGIS, instale as dependências via OSGeo4W:

``` sh
python -m pip install --upgrade pip
python -m pip install geopandas rasterio psutil requests
```

Antes de executar o processamento, baixe o arquivo TCLT disponível na pasta [auxiliary](https://github.com/migualex/cbers-wpm-1m/tree/main/auxiliary). Descompacte o arquivo e salve o `tclt.exe` em um local acessível no computador (ex: `C:\TCLT\tclt.exe`). O caminho para esse arquivo será informado dentro do plugin, na aba Processamento (passo 3.2).

## Referência

Fonseca, L. M. G., Prasad, G. S. S. D., & Mascarenhas, N. D. A. (1993).
Combined interpolation-restoration of Landsat images through FIR filter design techniques.
International Journal of Remote Sensing, 14(13), 2547–2561.

## Licença

Este projeto é distribuído sob a licença GNU General Public License v3.0.

Você é livre para usar, estudar, modificar e distribuir este software, desde 
que mantenha os avisos de copyright e a licença original em qualquer cópia 
ou trabalho derivado, conforme exigido pela GPL-3.0.
