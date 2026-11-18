\# 🎵 Miniplayer para Windows



Um miniplayer leve e personalizável para Windows 10 e Windows 11, desenvolvido em Python. Controle suas músicas e vídeos diretamente de uma interface compacta, flutuante ou integrada visualmente à barra de tarefas.



O aplicativo utiliza a sessão de mídia do próprio Windows para identificar e controlar conteúdos reproduzidos em diferentes programas, sem precisar abrir o aplicativo de origem.



\## ✨ Funcionalidades



\- \*\*Informações da mídia:\*\* exibição da capa, título, artista e aplicativo responsável pela reprodução.

\- \*\*Controles de reprodução:\*\* avançar, voltar, pausar e retomar músicas ou vídeos.

\- \*\*Barra de progresso:\*\* acompanhamento e navegação pela duração da mídia (seek).

\- \*\*Controle de volume:\*\* ajuste do volume geral do sistema, com opção de silenciar e reativar o áudio.

\- \*\*Dois modos de exibição:\*\*

&#x20; - Flutuante: janela compacta que pode ser posicionada livremente na tela.

&#x20; - Barra de tarefas: posicionamento do player sobre a barra de tarefas do Windows.

\- \*\*Personalização:\*\* temas claro e escuro, além de opção de transparência.

\- \*\*Inicialização automática:\*\* opção para iniciar o programa junto com o Windows.

\- \*\*Interface adaptável:\*\* suporte a diferentes escalas de DPI e cantos arredondados no Windows 11.



\## 🖥️ Compatibilidade



Desenvolvido para Windows 10 e Windows 11.



O Miniplayer utiliza a API Windows System Media Transport Controls (SMTC), permitindo interagir com sessões de mídia disponibilizadas pelo sistema operacional.



Alguns aplicativos compatíveis incluem:



\- Spotify

\- Google Chrome

\- Microsoft Edge

\- Mozilla Firefox

\- Brave

\- VLC Media Player

\- Windows Media Player



A compatibilidade depende da disponibilização de uma sessão de mídia pelo aplicativo de origem.



\## 📸 Demonstração



\*Imagens da interface serão adicionadas futuramente.\*



\## 📥 Download



Você pode baixar a versão executável do Miniplayer na seção \*\*Releases\*\* deste repositório.



1\. Acesse a página de Releases.

2\. Baixe o arquivo `Miniplayer.exe`.

3\. Execute o arquivo no Windows.



Não é necessário instalar o Python para utilizar a versão executável.



\## 🛠️ Instalação para desenvolvimento



Caso queira executar o projeto diretamente pelo código-fonte, siga os passos abaixo.



\### Pré-requisitos



\- Windows 10 ou Windows 11.

\- Python instalado.

\- Git (opcional, para clonar o repositório).



\### 1. Clone o repositório



```bash

git clone https://github.com/SEU-USUARIO/SEU-REPOSITORIO.git

```



Entre na pasta do projeto:



```bash

cd SEU-REPOSITORIO

```



\### 2. Instale as dependências



```bash

pip install -r requirements.txt

```



\### 3. Execute o programa



```bash

python miniplayer.py

```



\*\*Dica:\*\* para executar o programa sem abrir uma janela de terminal, você pode renomear o arquivo `miniplayer.py` para `miniplayer.pyw`.



\## 📂 Estrutura do projeto



```text

Miniplayer/

│

├── miniplayer.py       # Código-fonte

├── Miniplayer.exe      # Executável para Windows

├── requirements.txt    # Dependências do projeto

├── README.md           # Documentação

└── .gitignore          # Arquivos ignorados pelo Git

```



\## ⚙️ Tecnologias utilizadas



\- \*\*Python:\*\* linguagem principal do projeto.

\- \*\*Tkinter:\*\* construção da interface gráfica.

\- \*\*Pillow:\*\* processamento e exibição das capas dos álbuns.

\- \*\*PyCAW:\*\* controle do volume do sistema operacional.

\- \*\*Windows Runtime (WinRT):\*\* acesso às sessões de mídia do Windows.

\- \*\*CTypes:\*\* integração com funções nativas do Windows.



\## ⚠️ Observações



\- O programa foi desenvolvido especificamente para o sistema operacional Windows.

\- O funcionamento dos controles depende dos recursos disponibilizados pela sessão de mídia ativa.

\- O modo de integração à barra de tarefas foi desenvolvido considerando a barra de tarefas horizontal do Windows.

\- A opção de inicialização automática realiza alterações na chave de inicialização do usuário no Registro do Windows.



\## 📄 Licença



Este projeto está disponível sob a licença MIT.



Consulte o arquivo `LICENSE` para obter mais informações.



\---



Desenvolvido por Felipe Souza.

