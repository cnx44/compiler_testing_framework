#include <stdlib.h>


int main(int argc, char** args){
	if(argc != 3) return 1;
	
	int a = atoi(args[1]);
	int b = atoi(args[2]);
	int c = a + b;

	return 0;
}
