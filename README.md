# ASPIS testing tool

## Install dependencies

To install all the dependencies run the following command once you 
have created a virtual envoirment. We are using .venv for the folder

```shell
pip install -r requirements.txt
```

## Run tool's test

To run the tool's test run the following command in the root directory of the project
once you are in the virtual evoirment with all the dependencies

```shell
python3 -m unittest discover -s test -p "test_*.py" -v
```
