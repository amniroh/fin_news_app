import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../services/user_service.dart';
import '../models/onboarding_data.dart';
import 'home_screen.dart';

class OnboardingQuestionsScreen extends StatefulWidget {
  const OnboardingQuestionsScreen({super.key});

  @override
  State<OnboardingQuestionsScreen> createState() => _OnboardingQuestionsScreenState();
}

class _OnboardingQuestionsScreenState extends State<OnboardingQuestionsScreen> {
  int _currentStep = 0;
  
  // Form data
  int? _age;
  String? _incomeRange;
  List<String> _investmentGoals = [];
  int? _timeHorizon;
  int? _riskComfortLevel;
  int? _priorExperience;
  
  final List<String> _incomeRanges = [
    'Under \$30,000',
    '\$30,000 - \$50,000',
    '\$50,000 - \$100,000',
    '\$100,000 - \$150,000',
    'Over \$150,000',
  ];
  
  final List<String> _goalOptions = [
    'Retirement',
    'Buying a Home',
    'Emergency Fund',
    'Kids Education',
    'Just want to invest a little',
  ];
  
  final List<String> _riskScenarios = [
    'I\'m comfortable with ups and downs',
    'I prefer steady, predictable growth',
    'I get nervous when values drop',
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('Step ${_currentStep + 1} of 6'),
        backgroundColor: Colors.blue[700],
        foregroundColor: Colors.white,
      ),
      body: _buildStepContent(),
    );
  }

  Widget _buildStepContent() {
    switch (_currentStep) {
      case 0:
        return _buildAgeStep();
      case 1:
        return _buildIncomeStep();
      case 2:
        return _buildGoalsStep();
      case 3:
        return _buildTimeHorizonStep();
      case 4:
        return _buildRiskStep();
      case 5:
        return _buildExperienceStep();
      default:
        return const SizedBox();
    }
  }

  Widget _buildAgeStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'How old are you?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 24),
          SizedBox(
            height: 400,
            child: GridView.builder(
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 3,
                crossAxisSpacing: 16,
                mainAxisSpacing: 16,
              ),
              itemCount: 8,
              itemBuilder: (context, index) {
                final age = (index + 1) * 10;
                final isSelected = _age == age;
                return GestureDetector(
                  onTap: () => setState(() => _age = age),
                  child: Container(
                    decoration: BoxDecoration(
                      color: isSelected ? Colors.blue[700] : Colors.grey[200],
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Center(
                      child: Text(
                        '$age',
                        style: TextStyle(
                          color: isSelected ? Colors.white : Colors.black87,
                          fontSize: 20,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
          _buildNextButton(),
        ],
      ),
    );
  }

  Widget _buildIncomeStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'What\'s your income range?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 24),
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: _incomeRanges.length,
            itemBuilder: (context, index) {
              final range = _incomeRanges[index];
              final isSelected = _incomeRange == range;
              return Card(
                margin: const EdgeInsets.only(bottom: 12),
                color: isSelected ? Colors.blue[100] : Colors.white,
                child: ListTile(
                  title: Text(range),
                  onTap: () => setState(() => _incomeRange = range),
                  trailing: isSelected
                      ? Icon(Icons.check_circle, color: Colors.blue[700])
                      : null,
                ),
              );
            },
          ),
          const SizedBox(height: 24),
          _buildNextButton(),
        ],
      ),
    );
  }

  Widget _buildGoalsStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'What are your investment goals?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Select all that apply',
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
              color: Colors.grey[600],
            ),
          ),
          const SizedBox(height: 24),
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: _goalOptions.length,
            itemBuilder: (context, index) {
              final goal = _goalOptions[index];
              final isSelected = _investmentGoals.contains(goal);
              return Card(
                margin: const EdgeInsets.only(bottom: 12),
                color: isSelected ? Colors.blue[100] : Colors.white,
                child: ListTile(
                  title: Text(goal),
                  onTap: () {
                    setState(() {
                      if (isSelected) {
                        _investmentGoals.remove(goal);
                      } else {
                        _investmentGoals.add(goal);
                      }
                    });
                  },
                  trailing: isSelected
                      ? Icon(Icons.check_circle, color: Colors.blue[700])
                      : null,
                ),
              );
            },
          ),
          const SizedBox(height: 24),
          _buildNextButton(required: _investmentGoals.isNotEmpty),
        ],
      ),
    );
  }

  Widget _buildTimeHorizonStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'How many years until you need this money?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 24),
          SizedBox(
            height: 300,
            child: GridView.builder(
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 2,
                crossAxisSpacing: 16,
                mainAxisSpacing: 16,
              ),
              itemCount: 6,
              itemBuilder: (context, index) {
                final years = [1, 3, 5, 10, 15, 20][index];
                final isSelected = _timeHorizon == years;
                return GestureDetector(
                  onTap: () => setState(() => _timeHorizon = years),
                  child: Container(
                    decoration: BoxDecoration(
                      color: isSelected ? Colors.blue[700] : Colors.grey[200],
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Center(
                      child: Text(
                        '$years years',
                        style: TextStyle(
                          color: isSelected ? Colors.white : Colors.black87,
                          fontSize: 18,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
          const SizedBox(height: 24),
          _buildNextButton(),
        ],
      ),
    );
  }

  Widget _buildRiskStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Imagine your investments dropped 10% this month. How do you feel?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 24),
          ListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: _riskScenarios.length,
            itemBuilder: (context, index) {
              final scenario = _riskScenarios[index];
              final riskLevel = index + 1; // 1-3
              final isSelected = _riskComfortLevel == riskLevel;
              return Card(
                margin: const EdgeInsets.only(bottom: 12),
                color: isSelected ? Colors.blue[100] : Colors.white,
                child: ListTile(
                  title: Text(scenario),
                  onTap: () => setState(() => _riskComfortLevel = riskLevel),
                  trailing: isSelected
                      ? Icon(Icons.check_circle, color: Colors.blue[700])
                      : null,
                ),
              );
            },
          ),
          const SizedBox(height: 24),
          _buildNextButton(),
        ],
      ),
    );
  }

  Widget _buildExperienceStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24.0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'What\'s your investment experience?',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
              fontWeight: FontWeight.bold,
            ),
          ),
          const SizedBox(height: 24),
          Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              _buildExperienceOption(0, 'No experience - I\'m just starting'),
              const SizedBox(height: 16),
              _buildExperienceOption(1, 'Some experience - I\'ve invested a little'),
              const SizedBox(height: 16),
              _buildExperienceOption(2, 'Moderate experience - I\'m comfortable with basics'),
              const SizedBox(height: 16),
              _buildExperienceOption(3, 'Experienced - I understand most concepts'),
            ],
          ),
          const SizedBox(height: 24),
          _buildSubmitButton(),
        ],
      ),
    );
  }

  Widget _buildExperienceOption(int level, String label) {
    final isSelected = _priorExperience == level;
    return GestureDetector(
      onTap: () => setState(() => _priorExperience = level),
      child: Container(
        padding: const EdgeInsets.all(20),
        decoration: BoxDecoration(
          color: isSelected ? Colors.blue[700] : Colors.grey[200],
          borderRadius: BorderRadius.circular(12),
        ),
        child: Row(
          children: [
            Expanded(
              child: Text(
                label,
                style: TextStyle(
                  color: isSelected ? Colors.white : Colors.black87,
                  fontSize: 16,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
            if (isSelected)
              Icon(Icons.check_circle, color: Colors.white),
          ],
        ),
      ),
    );
  }

  Widget _buildNextButton({bool required = true}) {
    return Padding(
      padding: const EdgeInsets.only(top: 24),
      child: SizedBox(
        width: double.infinity,
        height: 50,
        child: ElevatedButton(
          onPressed: required ? _nextStep : null,
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.blue[700],
            foregroundColor: Colors.white,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
          ),
          child: const Text('Next'),
        ),
      ),
    );
  }

  Widget _buildSubmitButton() {
    return Padding(
      padding: const EdgeInsets.only(top: 24),
      child: SizedBox(
        width: double.infinity,
        height: 50,
        child: ElevatedButton(
          onPressed: _submitOnboarding,
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.blue[700],
            foregroundColor: Colors.white,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
          ),
          child: const Text('Complete Setup'),
        ),
      ),
    );
  }

  void _nextStep() {
    if (_currentStep < 5) {
      setState(() => _currentStep++);
    }
  }

  Future<void> _submitOnboarding() async {
    // Show loading dialog
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (context) => const Center(
        child: Card(
          child: Padding(
            padding: EdgeInsets.all(20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(),
                SizedBox(height: 16),
                Text('Saving your profile...'),
              ],
            ),
          ),
        ),
      ),
    );

    try {
      // Generate user ID if not exists
      String? userId = await UserService.getUserId();
      if (userId == null) {
        userId = 'user_${DateTime.now().millisecondsSinceEpoch}';
        await UserService.setUserId(userId);
      }

      final onboardingData = OnboardingData(
        age: _age,
        incomeRange: _incomeRange,
        investmentGoals: _investmentGoals,
        timeHorizon: _timeHorizon,
        riskComfortLevel: _riskComfortLevel,
        priorExperience: _priorExperience,
      );

      final response = await ApiService.saveOnboarding({
        'user_id': userId,
        ...onboardingData.toJson(),
      });

      await UserService.setOnboardingCompleted(true);

      if (mounted) {
        Navigator.of(context).pop(); // Close loading dialog
        Navigator.of(context).pushReplacement(
          MaterialPageRoute(
            builder: (context) => HomeScreen(suggestion: response['suggestion']),
          ),
        );
      }
    } catch (e) {
      if (mounted) {
        Navigator.of(context).pop(); // Close loading dialog
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Error: $e'),
            backgroundColor: Colors.red,
            duration: const Duration(seconds: 5),
            action: SnackBarAction(
              label: 'Retry',
              textColor: Colors.white,
              onPressed: _submitOnboarding,
            ),
          ),
        );
      }
    }
  }
}

